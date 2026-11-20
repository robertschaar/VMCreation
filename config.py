import os
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

PROXMOX_IP = os.getenv("PROXMOX_IP")
PROXMOX_NODE = os.getenv("PROXMOX_NODE")
TOKEN_USER = os.getenv("TOKEN_USER")
TOKEN_NAME = os.getenv("TOKEN_NAME")
TOKEN_SECRET = os.getenv("TOKEN_SECRET")

FTP_SERVER_IP = os.getenv("FTP_SERVER_IP")
COLD_STORAGE_SHARE = os.getenv("COLD_STORAGE_SHARE")
COLD_STORAGE_CREDS = os.getenv("COLD_STORAGE_CREDS")

GATEWAY = os.getenv("GATEWAY")
NETMASK_CIDR = os.getenv("NETMASK_CIDR")
NAMESERVER = os.getenv("NAMESERVER")
STORAGE_POOL = os.getenv("STORAGE_POOL")

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SENDER_EMAIL = SMTP_USER

FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
