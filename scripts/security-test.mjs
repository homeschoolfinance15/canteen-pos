const rawBase = process.env.BASE_URL || process.argv[2];

if (!rawBase) {
  throw new Error("Usage: BASE_URL=https://your-app.example.com npm run security:no-auth");
}

const base = rawBase.replace(/\/$/, "");

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(base + path, { ...options, headers, redirect: "manual" });
  return {
    status: response.status,
    text: await response.text()
  };
}

async function expectStatus(label, expected, path, options = {}) {
  const result = await request(path, options);
  if (!expected.includes(result.status)) {
    throw new Error(`${label}: expected ${expected.join(" or ")}, got ${result.status}: ${result.text.slice(0, 200)}`);
  }
  console.log(`ok ${label}: ${result.status}`);
}

async function expectLoginRejected(label, credentials) {
  const result = await request("/api/login", {
    method: "POST",
    body: JSON.stringify(credentials)
  });
  if (result.status === 200) {
    throw new Error(`${label}: default credentials successfully logged in`);
  }
  if (![401, 403, 429].includes(result.status)) {
    throw new Error(`${label}: expected 401, 403, or 429, got ${result.status}: ${result.text.slice(0, 200)}`);
  }
  console.log(`ok ${label}: ${result.status}`);
}

async function expectNoFileLeak(label, path) {
  const result = await request(path);
  const leaked = [
    "FastAPI(title=",
    "DATABASE_URL",
    "def frontend",
    "pbkdf2_sha256",
    "root:",
  ].some((marker) => result.text.includes(marker));
  if (leaked) {
    throw new Error(`${label}: possible local file disclosure at ${path}`);
  }
  console.log(`ok ${label}: no file leak`);
}

await expectStatus("public health", [200], "/api/health");
await expectStatus("anonymous session blocked", [401], "/api/session");
await expectStatus("anonymous state blocked", [401], "/api/state");
await expectStatus("anonymous workers blocked", [401], "/api/users");
await expectStatus("anonymous backup blocked", [401], "/api/backups");
await expectLoginRejected("default manager credentials rejected", {
  email: "owner@example.com",
  password: "changeme"
});
await expectStatus("anonymous account creation blocked", [401], "/api/accounts", {
  method: "POST",
  body: JSON.stringify({ name: "Security Test Account", openingBalance: 1 })
});
await expectStatus("anonymous sale blocked", [401], "/api/sales", {
  method: "POST",
  body: JSON.stringify({ accountId: "fake", lines: [{ itemId: "fake", quantity: 1 }] })
});
await expectStatus("cross-site account creation blocked", [403], "/api/accounts", {
  method: "POST",
  headers: { Origin: "https://evil.example" },
  body: JSON.stringify({ name: "Security Test Account", openingBalance: 1 })
});
await expectStatus("cross-site login blocked", [403], "/api/login", {
  method: "POST",
  headers: { Origin: "https://evil.example" },
  body: JSON.stringify({ email: "attacker@example.com", password: "wrong-password" })
});
await expectNoFileLeak("path traversal server.py blocked", "/%2e%2e/server.py");
await expectNoFileLeak("path traversal passwd blocked", "/%2e%2e/%2e%2e/%2e%2e/etc/passwd");

console.log("No-login security checks passed");
