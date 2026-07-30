"""Redeem device code, publish the bot app to the tenant catalog, install for the admin user."""
import json
import httpx

TENANT = "0f643a1e-e53d-4e57-8f28-ede605fb1aef"
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
G = "https://graph.microsoft.com/v1.0"
UPN = "admin@Yunda855.onmicrosoft.com"
ZIP = "deploy/teams-app/intelliknow-kms-teams-app.zip"

device = json.load(open("/tmp/devicecode2.json"))
r = httpx.post(
    f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": CLIENT_ID,
        "device_code": device["device_code"],
    },
    timeout=15,
)
body = r.json()
assert "access_token" in body, body.get("error_description", "token failed")[:300]
json.dump(body, open("/tmp/graph_token2.json", "w"))
token = body["access_token"]
H = {"Authorization": "Bearer " + token}

# 1. Publish (or find existing) app in the tenant catalog
r = httpx.post(
    G + "/appCatalogs/teamsApps",
    headers={**H, "Content-Type": "application/zip"},
    content=open(ZIP, "rb").read(),
    timeout=60,
)
if r.status_code in (200, 201):
    app = r.json()
    print("published:", app["id"], app.get("displayName"))
elif r.status_code == 409:
    print("already published, looking it up...")
    apps = httpx.get(
        G + "/appCatalogs/teamsApps?$filter=externalId eq '9c65cc4e-be62-40d1-8101-65f6afe6ee38'",
        headers=H, timeout=15,
    ).json()["value"]
    app = apps[0]
    print("found:", app["id"], app.get("displayName"))
else:
    print("publish FAILED:", r.status_code, r.text[:400])
    raise SystemExit(1)

# 2. Install the app for the admin user so the chat is pre-provisioned
r = httpx.post(
    f"{G}/users/{UPN}/teamwork/installedApps",
    headers={**H, "Content-Type": "application/json"},
    json={"teamsApp@odata.bind": f"{G}/appCatalogs/teamsApps/{app['id']}"},
    timeout=30,
)
print("install for user:", r.status_code,
      "OK" if r.status_code in (200, 201, 204) else r.text[:300])
