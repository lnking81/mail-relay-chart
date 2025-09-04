#!/usr/bin/env python3
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Email details
from_addr = "no-reply@app.blissyogaclub.com"
to_addr = "ilya@strukov.net"
subject = "Test Email from Mail Relay"

# Create message
msg = MIMEMultipart()
msg["From"] = from_addr
msg["To"] = to_addr
msg["Subject"] = subject
msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

# Email body
body = """This is a test email sent from the app.blissyogaclub.com mail relay.

The mail relay is working correctly and this email was sent via SMTP.

Technical details:
- Sent from: no-reply@app.blissyogaclub.com
- Mail server: mail.app.blissyogaclub.com
- DKIM signing: enabled
- Time: {}

Best regards,
Bliss Yoga Club Mail System
""".format(
    datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
)

msg.attach(MIMEText(body, "plain"))

try:
    # Connect to SMTP server
    print("Connecting to SMTP server localhost:2525...")
    server = smtplib.SMTP("localhost", 2525)

    # Enable debug output
    server.set_debuglevel(1)

    # Send email
    print(f"Sending email from {from_addr} to {to_addr}...")
    text = msg.as_string()
    server.sendmail(from_addr, [to_addr], text)
    server.quit()

    print("✅ Email sent successfully!")

except Exception as e:
    print(f"❌ Failed to send email: {e}")
