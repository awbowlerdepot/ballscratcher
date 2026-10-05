"""
Mint (or rotate) a Partner API key (DEPLOY_RUNBOOK.md 6cj).

    python3 scripts/create_partner_api_key.py <partner-name>
    python3 scripts/create_partner_api_key.py <partner-name> --revoke

Generates a random key, prints it ONCE to this terminal (hand it to the
partner over a secure channel -- it is not stored anywhere), and writes
only its SHA-256 hash into the Secrets Manager secret the partner API
authorizer reads, creating the secret on first use. Re-running for an
existing partner replaces (rotates) its key; --revoke removes the partner.
The authorizer re-reads the secret every few minutes, so changes take
effect without a deploy.

Run it locally with AWS credentials for the bowling-scraper account.
Never paste the printed key into chat, the repo, or a ticket.
"""
import argparse
import hashlib
import json
import re
import secrets
import sys

SECRET_ID = "bowling-scraper-partner-api-keys"
REGION = "us-west-1"
KEY_PREFIX = "biq_partner_"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("partner", help="Short partner name, e.g. 'acme-platform' (letters, digits, - and _)")
    parser.add_argument("--revoke", action="store_true", help="Remove this partner's key instead of minting one")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", args.partner):
        sys.exit("partner name must be 2-64 letters, digits, '-' or '_'")

    import boto3

    client = boto3.client("secretsmanager", region_name=REGION)
    try:
        data = json.loads(client.get_secret_value(SecretId=SECRET_ID)["SecretString"])
        exists = True
    except client.exceptions.ResourceNotFoundException:
        data, exists = {"keys": {}}, False
    keys = data.setdefault("keys", {})

    if args.revoke:
        if args.partner not in keys:
            sys.exit(f"no key for {args.partner!r}")
        del keys[args.partner]
        key = None
    else:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        keys[args.partner] = hashlib.sha256(key.encode("utf-8")).hexdigest()

    payload = json.dumps(data, sort_keys=True)
    if exists:
        client.put_secret_value(SecretId=SECRET_ID, SecretString=payload)
    else:
        client.create_secret(
            Name=SECRET_ID,
            Description="Partner API keys as SHA-256 hashes: {\"keys\": {partner: sha256hex}} (runbook 6cj)",
            SecretString=payload,
        )

    if key is None:
        print(f"Revoked {args.partner}. Takes effect within ~5 minutes.")
    else:
        print(f"Partner API key for {args.partner} (shown once, not stored):\n\n  {key}\n")
        print("Send it to the partner securely. It works within ~5 minutes.")


if __name__ == "__main__":
    main()
