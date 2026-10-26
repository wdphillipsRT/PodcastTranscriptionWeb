"""Private operator controls. Output is deliberately limited to safe aggregates."""
from functools import wraps
from types import SimpleNamespace

import click
from flask import current_app
from flask.cli import with_appcontext


def _safe(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except click.ClickException:
            raise
        except Exception:
            from extensions import db
            db.session.rollback()
            raise click.ClickException("Classification operation failed") from None
    return call


def _integer(value, *, maximum=2**31 - 1):
    try:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError
        result = int(value)
        if not 1 <= result <= maximum:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise click.ClickException("Invalid numeric option") from None


@click.group("classification")
@with_appcontext
def classification_cli():
    """Inspect and operate transcript classification without revealing content."""


@classification_cli.command("status")
@_safe
def status():
    import sqlalchemy as sa
    from extensions import db
    from datetime import timezone
    from models import ClassificationBlock, ClassificationRevision, CLASSIFICATION_STATES
    from tasks import _missing_classification_query
    counts = {state: count for state, count in db.session.execute(sa.select(ClassificationRevision.state, sa.func.count())
        .group_by(ClassificationRevision.state))}
    review = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision)
        .where(ClassificationRevision.review_required.is_(True)))
    missing = db.session.scalar(sa.select(sa.func.count()).select_from(_missing_classification_query().subquery()))
    # Busy retries never end by design; these show how many wait and since when.
    busy = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision).where(
        ClassificationRevision.state == "retrying", ClassificationRevision.error_category == "router_busy"))
    sent, updated = db.session.execute(sa.select(sa.func.min(ClassificationBlock.request_generation_started_at),
        sa.func.min(ClassificationBlock.updated_at)).where(ClassificationBlock.state == "pending",
        ClassificationBlock.error_category == "router_busy")).one()
    queued = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision).where(
        ClassificationRevision.state == "pending", ClassificationRevision.error_category == "reclassify_queued"))
    db.session.commit()

    def stamp(value):
        return "none" if value is None else (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    click.echo(" ".join(f"{state}={counts.get(state, 0)}" for state in CLASSIFICATION_STATES)
        + f" review_required={review} missing_current_policy={missing}"
        + f" router_busy={busy} oldest_busy_key_sent={stamp(sent)} oldest_busy_updated={stamp(updated)}"
        + f" reclassify_queued={queued}")


@classification_cli.command("enqueue-backlog")
@click.option("--limit", default="100", metavar="1-100")
@_safe
def enqueue_backlog(limit):
    from tasks import reconcile_classification_batch
    counts = reconcile_classification_batch(limit=_integer(limit, maximum=100), create_only=True)
    click.echo(" ".join(f"{name}={counts[name]}" for name in ("scanned", "created", "published", "failed")))


@classification_cli.command("retry")
@click.option("--item-id", required=True)
@_safe
def retry(item_id):
    from extensions import db
    from models import ContentItem, ClassificationRevision
    from tasks import _item_lock, get_classification_service, _revision_dispatch, _publish_classification
    identity = _integer(item_id)
    with _item_lock(identity) as acquired:
        if not acquired:
            raise click.ClickException("Classification work is busy")
        item = db.session.get(ContentItem, identity)
        if item is None or item.status != "completed":
            raise click.ClickException("Completed item required")
        failed = db.session.query(ClassificationRevision).filter_by(item_id=identity,
            transcript_sha256=item.transcript_sha256, state="failed").order_by(ClassificationRevision.id.desc()).first()
        if failed is None:
            raise click.ClickException("Failed classification required")
        key = f"manual_retry:{failed.id}"
        db.session.commit()
        revision_id = get_classification_service().ensure_revision(identity, "manual_retry", idempotency_key=key)
        revision = db.session.get(ClassificationRevision, revision_id)
        dispatch = _revision_dispatch(revision) if revision.error_category == "dispatch_pending" else None
        db.session.commit()
    published = _publish_classification(dispatch)
    click.echo(f"requested=1 published={int(published)}")


@classification_cli.command("router-smoke")
@click.option("--wrong-client-cert", is_flag=True,
              help="Send the request with a throwaway client certificate; pass only if it is refused as model_auth.")
@_safe
def router_smoke(wrong_client_cert):
    """One small synthetic request on podcast.classification; touches no database or storage data."""
    import time
    from commercial_signals import ClassifierWindow
    from router_classifier import ClassifierError, RouterClassifier
    from router_client import RouterClient
    from transcripts import TranscriptSegment
    settings = SimpleNamespace(**dict(current_app.config))
    client = RouterClient.from_settings(settings)
    if not client.ready():
        raise click.ClickException("Router is not ready (GET /readyz did not answer ready)")
    # This fixture is compiled in and has no connection to database/storage data.
    segment = TranscriptSegment("seg-000001-aaaaaaaaaaaaaaaa", "a" * 64, 0.0, 1.0,
        "This synthetic example explains how rain forms from clouds.", "https://example.com/synthetic#t=0,1")
    window = ClassifierWindow(1, (segment,), (), (), ())
    if wrong_client_cert:
        _wrong_client_cert_smoke(settings, window)
        return
    started = time.monotonic()
    try:
        result = RouterClassifier(settings, client=client).classify(window)
    except ClassifierError as error:
        raise click.ClickException(f"Router smoke failed: {error.category} reason={error.reason}") from None
    click.echo(f"ready=1 model={result.provenance.resolved_model} label={result.decisions[0].label} "
               f"seconds={time.monotonic() - started:.1f}")


@classification_cli.command("compare")
@click.option("--sample", default="20", metavar="1-100")
@click.option("--seed", default="20261026", metavar="1-2147483647")
@click.option("--max-blocks", default="4", metavar="1-50")
@_safe
def compare(sample, seed, max_blocks):
    """Classify a fixed sample of Claude-classified items with the router model; stores nothing."""
    import json
    from classification_compare import compare_models
    from extensions import db
    from router_classifier import ClassifierError
    from tasks import get_classification_service
    sample, seed, max_blocks = _integer(sample, maximum=100), _integer(seed), _integer(max_blocks, maximum=50)
    try:
        report = compare_models(session=db.session, service=get_classification_service(), sample=sample,
                                seed=seed, max_blocks=max_blocks)
    except ClassifierError as error:
        raise click.ClickException(f"Comparison stopped: {error.category}") from None
    click.echo(json.dumps(report, sort_keys=True, indent=2))


@classification_cli.command("reclassify-all")
@click.option("--limit", default=None, metavar="1-100000")
@_safe
def reclassify_all(limit):
    """Queue every completed item without a current-classifier revision, newest first; the classifier works one at a time."""
    from tasks import _publish_classification, queue_reclassification, release_reclassification
    bound = _integer(limit, maximum=100_000) if limit is not None else None
    counts = queue_reclassification(limit=bound)
    released = _publish_classification(release_reclassification())
    click.echo(" ".join(f"{name}={counts[name]}" for name in ("candidates", "queued", "busy", "failed"))
               + f" released={int(released)}")


def _wrong_client_cert_smoke(settings, window):
    """The suite never exercises TLS, so this proves against the real router that a client certificate
    it did not issue is refused and mapped to model_auth. Prints only the category and reason."""
    import os
    import shutil
    import tempfile
    from router_classifier import ClassifierError, RouterClassifier
    from router_client import RouterClient
    directory = tempfile.mkdtemp(prefix="router-smoke-")  # mode 0700
    try:
        certificate, key = os.path.join(directory, "client.crt"), os.path.join(directory, "client.key")
        _write_throwaway_client_certificate(certificate, key)
        wrong = RouterClient.from_settings(SimpleNamespace(**{**vars(settings), "ROUTER_CLIENT_CERT_FILE": certificate,
                                                              "ROUTER_CLIENT_KEY_FILE": key}))
        category = reason = None
        try:
            RouterClassifier(settings, client=wrong).classify(window)
        except ClassifierError as error:
            category, reason = error.category, error.reason
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    line = f"wrong-client-cert={category or 'accepted'} reason={reason or 'none'}"
    # "tls" when the TLS layer refuses the certificate; unauthorized/forbidden if a proxy answers instead.
    if category != "model_auth" or reason not in {"tls", "unauthorized", "forbidden"}:
        raise click.ClickException(line)
    click.echo(line)


def _der(tag, body):
    if len(body) < 0x80:
        return bytes((tag, len(body))) + body
    size = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
    return bytes((tag, 0x80 | len(size))) + size + body


def _der_integer(value):
    return _der(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def _probable_prime(bits):
    import secrets
    small = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97)
    while True:
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if any(candidate % prime == 0 for prime in small) or candidate % 65537 == 1:
            continue
        odd, twos = candidate - 1, 0
        while odd % 2 == 0:
            odd, twos = odd // 2, twos + 1
        for _ in range(32):
            value = pow(secrets.randbelow(candidate - 3) + 2, odd, candidate)
            if value in (1, candidate - 1):
                continue
            for _ in range(twos - 1):
                value = pow(value, 2, candidate)
                if value == candidate - 1:
                    break
            else:
                break
        else:
            return candidate


def _write_throwaway_client_certificate(certificate_path, key_path):
    """Write a fresh self-signed RSA-2048 certificate and PKCS#1 key (pure Python: the image has neither
    the openssl CLI nor a crypto library). No router trusts it; it exists only to be refused."""
    import base64
    import hashlib
    import os
    import secrets
    from datetime import datetime, timedelta, timezone
    exponent = 65537
    while True:
        p, q = _probable_prime(1024), _probable_prime(1024)
        if p != q and (p * q).bit_length() == 2048:
            break
    modulus, phi = p * q, (p - 1) * (q - 1)
    private = pow(exponent, -1, phi)
    sha256_rsa = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d01010b")) + b"\x05\x00")
    rsa = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    name = _der(0x30, _der(0x31, _der(0x30, _der(0x06, bytes.fromhex("550403"))
                                      + _der(0x0c, b"router-smoke wrong client certificate"))))
    now = datetime.now(timezone.utc)
    validity = _der(0x30, b"".join(_der(0x17, moment.strftime("%y%m%d%H%M%SZ").encode())
                                  for moment in (now - timedelta(hours=1), now + timedelta(days=1))))
    public = _der(0x30, rsa + _der(0x03, b"\x00" + _der(0x30, _der_integer(modulus) + _der_integer(exponent))))
    tbs = _der(0x30, _der_integer(secrets.randbits(63) + 1) + sha256_rsa + name + validity + name + public)
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(tbs).digest()
    padded = b"\x00\x01" + b"\xff" * (256 - 3 - len(digest_info)) + b"\x00" + digest_info
    signature = pow(int.from_bytes(padded, "big"), private, modulus).to_bytes(256, "big")
    certificate = _der(0x30, tbs + sha256_rsa + _der(0x03, b"\x00" + signature))
    key = _der(0x30, b"".join(_der_integer(value) for value in (
        0, modulus, exponent, private, p, q, private % (p - 1), private % (q - 1), pow(q, -1, p))))

    def pem(label, der):
        lines = [base64.b64encode(der[start:start + 48]).decode() for start in range(0, len(der), 48)]
        return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----", ""])

    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(pem("RSA PRIVATE KEY", key))
    with open(certificate_path, "x", encoding="ascii") as handle:
        handle.write(pem("CERTIFICATE", certificate))


@classification_cli.command("smoke")
@click.option("--item-id")
@click.option("--oldest-completed", is_flag=True)
@_safe
def smoke(item_id, oldest_completed):
    if (item_id is not None) == oldest_completed:
        raise click.ClickException("Select exactly one completed item")
    import time
    from extensions import db
    from models import ContentItem, ClassificationRevision
    from commercial_policy import MAX_BLOCKS_PER_REVISION
    from tasks import _item_lock, get_classification_service, _revision_dispatch, _classification_step
    identity = _integer(item_id) if item_id is not None else db.session.query(ContentItem.id).filter_by(
        status="completed").order_by(ContentItem.created_at, ContentItem.id).limit(1).scalar()
    db.session.commit()
    if identity is None:
        raise click.ClickException("Completed item required")
    with _item_lock(identity) as acquired:
        if not acquired:
            raise click.ClickException("Classification work is busy")
        item = db.session.get(ContentItem, identity)
        if item is None or item.status != "completed":
            raise click.ClickException("Completed item required")
        db.session.commit()
        service = get_classification_service()
        revision_id = service.ensure_revision(identity, "live_smoke")
        revision = db.session.get(ClassificationRevision, revision_id)
        dispatch = _revision_dispatch(revision)
        db.session.commit()
        for _ in range(MAX_BLOCKS_PER_REVISION + 2):
            if dispatch is None:
                break
            dispatch, countdown, _ = _classification_step(service, revision_id, dispatch.phase,
                dispatch.block_ordinal, handoff_superseded=False)
            if dispatch is not None and countdown:
                time.sleep(min(countdown, 600))  # A busy router: wait as the worker would.
        revision = db.session.get(ClassificationRevision, revision_id, populate_existing=True)
        if revision.state not in {"completed", "failed", "superseded"}:
            db.session.commit()
            raise click.ClickException("Classification smoke did not complete")
        output = (f"state={revision.state} included={revision.included_segments} excluded={revision.excluded_segments} "
            f"uncertain={revision.uncertain_segments}")
        db.session.commit()
    click.echo(output)
