import os
from twilio.rest import Client

# Load credentials from environment variables
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")
TWILIO_TO = os.getenv("TWILIO_TO")

def main():
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        print("❌ Missing one or more Twilio environment variables.")
        return

    client = Client(TWILIO_SID, TWILIO_TOKEN)

    try:
        message = client.messages.create(
            body="✅ Twilio test: your job alert bot can send SMS!",
            from_=TWILIO_FROM,
            to=TWILIO_TO
        )
        print(f"Message sent! SID: {message.sid}")
    except Exception as e:
        print(f"❌ Error sending message: {e}")

if __name__ == "__main__":
    main()
