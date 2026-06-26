const base = process.env.BASE_URL || "http://127.0.0.1:8000";
const managerEmail = process.env.SMOKE_MANAGER_EMAIL || process.env.SMOKE_OWNER_EMAIL || "owner@example.com";
const managerPassword = process.env.SMOKE_MANAGER_PASSWORD || process.env.SMOKE_OWNER_PASSWORD || "password123";

let cookie = "";

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (cookie) headers.Cookie = cookie;

  const response = await fetch(base + path, { ...options, headers });
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];

  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!response.ok) {
    const detail = typeof data === "object" ? data.detail || data.error || JSON.stringify(data) : data;
    const error = new Error(`${response.status} ${detail}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function expectStatus(label, status, action) {
  try {
    await action();
    throw new Error(`${label}: expected ${status} but request succeeded`);
  } catch (error) {
    if (error.status !== status) {
      throw new Error(`${label}: expected ${status}, got ${error.status || error.message}`);
    }
  }
}

function unique(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

await api("/api/login", {
  method: "POST",
  body: JSON.stringify({ email: managerEmail, password: managerPassword })
});

const accountName = unique("Camper");
const account = await api("/api/accounts", {
  method: "POST",
  body: JSON.stringify({ name: accountName, openingBalance: 20, note: "Cabin 1" })
});
await expectStatus("duplicate account name rejected", 400, () =>
  api("/api/accounts", {
    method: "POST",
    body: JSON.stringify({ name: accountName.toUpperCase(), openingBalance: 5 })
  })
);
const updatedAccountName = unique("Camper Updated");
await api(`/api/accounts/${account.id}`, {
  method: "PATCH",
  body: JSON.stringify({ name: updatedAccountName, note: "Cabin 2" })
});
const lowAccount = await api("/api/accounts", {
  method: "POST",
  body: JSON.stringify({ name: unique("Low Balance"), openingBalance: 1 })
});
const itemName = unique("Pretzel");
const item = await api("/api/items", {
  method: "POST",
  body: JSON.stringify({
    name: itemName,
    category: "Snacks",
    price: 2.5,
    quantity: 5,
    lowStockAt: 2,
    active: true
  })
});
await expectStatus("duplicate product name rejected", 400, () =>
  api("/api/items", {
    method: "POST",
    body: JSON.stringify({
      name: itemName.toUpperCase(),
      category: "Snacks",
      price: 2.5,
      quantity: 1,
      lowStockAt: 2,
      active: true
    })
  })
);

const secondManagerEmail = `${unique("manager")}@example.com`;
const cashierEmail = `${unique("cashier")}@example.com`;
await api("/api/users", {
  method: "POST",
  body: JSON.stringify({ email: secondManagerEmail, name: "Manager", password: "password123", role: "manager" })
});
await api("/api/users", {
  method: "POST",
  body: JSON.stringify({ email: cashierEmail, name: "Cashier", password: "password123", role: "cashier" })
});

await api("/api/logout", { method: "POST" });
await api("/api/login", {
  method: "POST",
  body: JSON.stringify({ email: cashierEmail, password: "password123" })
});

await expectStatus("cashier add funds forbidden", 403, () =>
  api(`/api/accounts/${account.id}/funds/add`, {
    method: "POST",
    body: JSON.stringify({ amount: 3, note: "cash" })
  })
);
await expectStatus("cashier ledger forbidden", 403, () => api("/api/ledger"));
await expectStatus("duplicate stock request rejected", 400, () =>
  api("/api/sales", {
    method: "POST",
    body: JSON.stringify({
      accountId: account.id,
      lines: [
        { itemId: item.id, quantity: 3 },
        { itemId: item.id, quantity: 3 }
      ]
    })
  })
);
await expectStatus("cashier negative override forbidden", 403, () =>
  api("/api/sales", {
    method: "POST",
    body: JSON.stringify({
      accountId: lowAccount.id,
      allowNegative: true,
      lines: [{ itemId: item.id, quantity: 1 }]
    })
  })
);
await api("/api/sales", {
  method: "POST",
  body: JSON.stringify({
    accountId: account.id,
    lines: [{ itemId: item.id, quantity: 2 }],
    note: "snack sale"
  })
});

await api("/api/logout", { method: "POST" });
await api("/api/login", {
  method: "POST",
  body: JSON.stringify({ email: secondManagerEmail, password: "password123" })
});
await expectStatus("blank balance reason rejected", 400, () =>
  api(`/api/accounts/${account.id}/funds/add`, {
    method: "POST",
    body: JSON.stringify({ amount: 1, note: "   " })
  })
);
await api(`/api/accounts/${account.id}/funds/add`, {
  method: "POST",
  body: JSON.stringify({ amount: 1, note: "cash top-up" })
});

await api("/api/logout", { method: "POST" });
await api("/api/login", {
  method: "POST",
  body: JSON.stringify({ email: managerEmail, password: managerPassword })
});

const state = await api("/api/state");
const soldAccount = state.accounts.find((entry) => entry.id === account.id);
const soldItem = state.items.find((entry) => entry.id === item.id);
if (soldAccount.balance !== 16) {
  throw new Error(`expected account balance 16, got ${soldAccount.balance}`);
}
if (soldAccount.name !== updatedAccountName || soldAccount.note !== "Cabin 2") {
  throw new Error("account name or note update missing");
}
if (soldItem.quantity !== 3) {
  throw new Error(`expected stock 3, got ${soldItem.quantity}`);
}
const purchaseTxn = state.transactions.find((txn) => txn.type === "purchase" && txn.items.length === 1 && txn.accountId === account.id);
if (!purchaseTxn) {
  throw new Error("purchase ledger with sale line missing");
}
await api(`/api/transactions/${purchaseTxn.id}/undo`, {
  method: "POST",
  body: JSON.stringify({ note: "Smoke test undo" })
});
const undone = await api("/api/state");
const undoneAccount = undone.accounts.find((entry) => entry.id === account.id);
const undoneItem = undone.items.find((entry) => entry.id === item.id);
if (undoneAccount.balance !== 21) {
  throw new Error(`expected account balance 21 after undo, got ${undoneAccount.balance}`);
}
if (undoneItem.quantity !== 5) {
  throw new Error(`expected stock 5 after undo, got ${undoneItem.quantity}`);
}
if (!undone.transactions.some((txn) => txn.type === "transaction_undone" && txn.details?.undoOf === purchaseTxn.id)) {
  throw new Error("undo ledger entry missing");
}

const dailyReports = await api("/api/reports/daily");
const newestReport = dailyReports.reports?.[0];
if (!newestReport) {
  throw new Error("daily report missing");
}
const timeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: dailyReports.timeZone,
  hour: "numeric",
  minute: "2-digit"
});
if (timeFormatter.format(new Date(newestReport.startsAt)) !== "12:00 AM") {
  throw new Error(`daily report starts at ${newestReport.startsAt}, expected local midnight`);
}
if (timeFormatter.format(new Date(newestReport.endsAt)) !== "12:00 AM") {
  throw new Error(`daily report ends at ${newestReport.endsAt}, expected next local midnight`);
}

const backup = await api("/api/backups");
if (!backup.users?.length || !backup.accounts?.length || !backup.items?.length || !backup.transactions?.length) {
  throw new Error("backup missing expected data");
}
await expectStatus("malformed backup rejected", 400, () =>
  api("/api/backups", { method: "POST", body: JSON.stringify({ accounts: [], items: [] }) })
);
await api("/api/backups", { method: "POST", body: JSON.stringify(backup) });
const restored = await api("/api/state");
if (!restored.accounts.some((entry) => entry.id === account.id)) {
  throw new Error("restored account missing");
}

console.log("API smoke test passed");
