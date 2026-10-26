"""Repository-wide pytest collection rules.

The vendored bezalel-rfc suite under integrations/knowledge/vendor/ runs only
through tests/test_knowledge_contract_vendor.py, against the vendored oracle.
"""

collect_ignore_glob = ["integrations/knowledge/vendor/*"]
