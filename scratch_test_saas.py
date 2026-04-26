import requests
import json

def test_saas_health(url, api_key):
    print(f"Testing SaaS Connection to {url}...")
    try:
        resp = requests.get(
            f"{url}/v1/health",
            headers={"X-API-Key": api_key},
            timeout=5
        )
        print(f"Status Code: {resp.status_code}")
        if resp.status_code == 200:
            print("✅ Connection Successful!")
            print(f"Response: {json.dumps(resp.json(), indent=2)}")
        else:
            print(f"❌ Connection Failed: {resp.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

# Esto es lo que haría Odoo internamente
if __name__ == "__main__":
    # Valores de ejemplo (el usuario deberá usar sus reales o yo puedo probar si levanto el server)
    test_saas_health("http://localhost:8000", "sk_cert_test_key_12345")
