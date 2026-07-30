"""Refresh Graph token, inspect SKUs, and assign the M365 license to the admin user."""
import json
import httpx

TENANT = "0f643a1e-e53d-4e57-8f28-ede605fb1aef"
CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
G = "https://graph.microsoft.com/v1.0"
UPN = "admin@Yunda855.onmicrosoft.com"

tok = json.load(open("/tmp/graph_token.json"))
r = httpx.post(
    f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
    data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": tok["refresh_token"],
        "scope": "https://graph.microsoft.com/.default offline_access",
    },
    timeout=15,
)
body = r.json()
assert "access_token" in body, body.get("error_description", "refresh failed")[:300]
json.dump(body, open("/tmp/graph_token.json", "w"))
H = {"Authorization": "Bearer " + body["access_token"], "Content-Type": "application/json"}

skus = httpx.get(G + "/subscribedSkus", headers=H, timeout=15).json()["value"]
target = None
for s in skus:
    plans = [p["servicePlanName"] for p in s["servicePlans"]]
    teams = [p for p in plans if "TEAMS" in p.upper()]
    used = str(s["consumedUnits"]) + "/" + str(s["prepaidUnits"]["enabled"])
    print("SKU:", s["skuPartNumber"], used, "teams_plans:", teams or "NONE")
    if s["prepaidUnits"]["enabled"] > s["consumedUnits"]:
        target = s

if target is None:
    print("no SKU with free units yet (provisioning may lag a few minutes)")
    raise SystemExit(0)

r = httpx.post(
    G + "/users/" + UPN + "/assignLicense",
    headers=H,
    json={"addLicenses": [{"skuId": target["skuId"], "disabledPlans": []}], "removeLicenses": []},
    timeout=20,
)
print("assignLicense:", r.status_code, "OK" if r.status_code == 200 else r.text[:300])
