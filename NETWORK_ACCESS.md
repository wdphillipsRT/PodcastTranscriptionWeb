# Network Access Configuration

This document explains how to access the Podcast Transcription Web application from other computers on your local network.

## Current Configuration

The application is now configured to listen on all network interfaces (0.0.0.0), making it accessible from:

- **Local machine**: http://127.0.0.1:5000 or http://localhost:5000
- **Network access**: http://192.168.68.107:5000 (from other devices on the same network)

## Accessing from Other Computers

### From the Same Local Network

Any device on your local network (192.168.68.x) can access the application by visiting:

```
http://192.168.68.107:5000
```

This includes:
- Other computers (Windows, Mac, Linux)
- Smartphones and tablets
- Smart TVs or other network devices

### Step-by-Step Access Instructions

1. **Ensure the application is running**
   ```bash
   # On the host machine
   python app.py
   ```

2. **Find your local IP address** (if it changes)
   ```bash
   hostname -I | awk '{print $1}'
   ```

3. **On another device on the same network:**
   - Open a web browser
   - Navigate to: `http://192.168.68.107:5000`
   - You should see the Podcast Transcription homepage

## Firewall Configuration

### Ubuntu/Debian (UFW)

If you have UFW firewall enabled, you may need to allow port 5000:

```bash
# Check firewall status
sudo ufw status

# If active, allow port 5000
sudo ufw allow 5000/tcp

# Verify the rule was added
sudo ufw status numbered
```

### Other Linux Distributions

**Firewalld (CentOS/RHEL/Fedora):**
```bash
sudo firewall-cmd --permanent --add-port=5000/tcp
sudo firewall-cmd --reload
```

**iptables (Manual):**
```bash
sudo iptables -A INPUT -p tcp --dport 5000 -j ACCEPT
sudo iptables-save > /etc/iptables/rules.v4
```

## Docker Network Access

If using Docker Compose (port 8005), the application is accessible at:

```
http://192.168.68.107:8005
```

The docker-compose.yml is already configured to expose port 8005:
```yaml
ports:
  - "8005:8005"
```

This binds to 0.0.0.0:8005 by default, making it network-accessible.

## Security Considerations

⚠️ **Important Security Notes:**

1. **Development Server Warning**:
   - Flask's built-in server is NOT suitable for production
   - Only use for development or trusted local networks
   - For production, use Gunicorn or Docker deployment

2. **Network Exposure**:
   - The application is now accessible to anyone on your local network
   - Ensure you trust all devices on your network
   - Consider using authentication (already implemented in the app)

3. **Production Deployment**:
   - For production, use a proper WSGI server (Gunicorn)
   - Configure a reverse proxy (Nginx/Apache)
   - Enable HTTPS with SSL certificates
   - Use environment-based configuration
   - Enable firewall rules appropriately

## Production Deployment with Gunicorn

For more secure network access, use Gunicorn:

```bash
# Install gunicorn (already in requirements.txt)
pip install gunicorn

# Run with Gunicorn
gunicorn --bind 0.0.0.0:8005 --workers 2 --timeout 120 app:app
```

Then access at: `http://192.168.68.107:8005`

## Troubleshooting

### Cannot Connect from Other Devices

1. **Check the application is running:**
   ```bash
   ss -tlnp | grep :5000
   ```
   Should show: `0.0.0.0:5000`

2. **Verify IP address:**
   ```bash
   hostname -I
   ```

3. **Test local connectivity first:**
   ```bash
   curl http://192.168.68.107:5000
   ```

4. **Check firewall:**
   ```bash
   sudo ufw status
   sudo iptables -L -n | grep 5000
   ```

5. **Ping test:**
   ```bash
   # From another device
   ping 192.168.68.107
   ```

### Port Already in Use

If you get "Address already in use" error:

```bash
# Find what's using port 5000
sudo lsof -i :5000

# Kill the process (replace PID)
kill <PID>

# Or use a different port
export FLASK_RUN_PORT=5001
python app.py
```

## Network Discovery

### Using mDNS/Avahi (Optional)

To access via hostname instead of IP:

1. **Install Avahi** (if not installed):
   ```bash
   sudo apt-get install avahi-daemon
   ```

2. **Access via hostname:**
   ```
   http://your-hostname.local:5000
   ```

3. **Find your hostname:**
   ```bash
   hostname
   ```

## Testing Network Access

### From Another Computer

**Windows:**
```cmd
# Open Command Prompt
curl http://192.168.68.107:5000
# Or just open in browser
```

**Mac/Linux:**
```bash
curl http://192.168.68.107:5000
# Should return HTML content
```

**Smartphone:**
- Connect to same WiFi network
- Open browser app
- Navigate to: http://192.168.68.107:5000

## Current Server Information

- **Host IP**: 192.168.68.107
- **Port**: 5000
- **Binding**: 0.0.0.0 (all interfaces)
- **Access URLs**:
  - Local: http://localhost:5000
  - Network: http://192.168.68.107:5000

## Reverting to Localhost Only

If you want to restrict access to localhost only:

1. **Edit app.py:**
   ```python
   # Change from:
   app.run(host='0.0.0.0', port=5000, debug=True)

   # Back to:
   app.run(host='127.0.0.1', port=5000, debug=True)
   # Or simply:
   app.run(debug=True)
   ```

2. **Restart the application**

## Docker Network Configuration

For Docker deployments, the network access is controlled in docker-compose.yml:

```yaml
web:
  ports:
    - "8005:8005"  # Host:Container - accessible on all interfaces
    # To restrict to localhost only:
    # - "127.0.0.1:8005:8005"
```

## Summary

✅ **Application is now network-accessible**
- Local access: http://localhost:5000
- Network access: http://192.168.68.107:5000
- All devices on 192.168.68.x subnet can connect

🔒 **Security reminders:**
- Only for trusted local networks
- Production should use Gunicorn + reverse proxy
- Consider enabling firewall rules
- Authentication is already in place (login required)

📝 **Quick test from another device:**
```
Open browser → http://192.168.68.107:5000 → Should see app homepage
```
