import socket, urllib.request

host = "api.telegram.org"
print("DNS results:")
for r in socket.getaddrinfo(host, 443):
    print(" ", r[0].name, r[4])

print("\nTesting HTTPS connect...")
try:
    resp = urllib.request.urlopen(f"https://{host}", timeout=5)
    print("OK:", resp.status)
except Exception as e:
    print("FAIL:", e)
