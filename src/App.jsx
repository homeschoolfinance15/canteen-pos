import { useEffect, useState } from "react";
import { api, formatDate, formatMoney, roleAtLeast } from "./api.js";

const nav = [
  { id: "register", label: "Register", min: "cashier" },
  { id: "accounts", label: "Accounts", min: "manager" },
  { id: "inventory", label: "Inventory", min: "manager" },
  { id: "reports", label: "Reports", min: "manager" },
  { id: "ledger", label: "Ledger", min: "manager" },
  { id: "refunds", label: "Refunds", min: "manager" },
  { id: "workers", label: "Workers", min: "manager" },
  { id: "data", label: "Data", min: "manager" }
];

const itemFields = [
  { key: "name", label: "Name", placeholder: "Product name", type: "text" },
  { key: "category", label: "Category", placeholder: "Category", type: "text" },
  { key: "price", label: "Price", placeholder: "0.00", type: "number", step: "0.01", min: "0" },
  { key: "quantity", label: "Qty", placeholder: "0", type: "number", step: "1", min: "0" },
  { key: "lowStockAt", label: "Low stock", placeholder: "5", type: "number", step: "1", min: "0" }
];

function displayRole(role) {
  return role === "owner" ? "manager" : role;
}

function blankState() {
  return { accounts: [], items: [], transactions: [], dailyReports: [], reportTimeZone: "", currentUser: null };
}

export default function App() {
  const [session, setSession] = useState(null);
  const [state, setState] = useState(blankState);
  const [view, setView] = useState("register");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [registerAccountId, setRegisterAccountId] = useState("");
  const [registerAccountQuery, setRegisterAccountQuery] = useState("");

  async function refresh() {
    const data = await api("/api/state");
    setState(data);
    setSession(data.currentUser);
    const allowed = nav.filter((item) => roleAtLeast(data.currentUser, item.min));
    if (!allowed.some((item) => item.id === view)) setView("register");
  }

  useEffect(() => {
    api("/api/session")
      .then((data) => {
        setSession(data.user);
        return refresh();
      })
      .catch(() => setSession(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!state.accounts.length) {
      setRegisterAccountId("");
      return;
    }
    if (!state.accounts.some((item) => item.id === registerAccountId)) {
      setRegisterAccountId(state.accounts[0].id);
    }
  }, [registerAccountId, state.accounts]);

  async function run(action, message) {
    setError("");
    try {
      await action();
      await refresh();
      if (message) {
        setToast(message);
        setTimeout(() => setToast(""), 2400);
      }
      return true;
    } catch (err) {
      setError(err.message || "Something went wrong");
      return false;
    }
  }

  async function logout() {
    await api("/api/logout", { method: "POST" });
    setSession(null);
    setState(blankState());
  }

  if (loading) return <Shell status="Loading canteen..." />;
  if (!session) {
    return (
      <Login
        onLogin={async () => {
          setLoading(true);
          await refresh();
          setLoading(false);
        }}
      />
    );
  }

  const visibleNav = nav.filter((item) => roleAtLeast(session, item.min));

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span>C</span>
          <div>
            <strong>Canteen POS</strong>
            <small>{session.name} · {displayRole(session.role)}</small>
          </div>
        </div>
        <nav>
          {visibleNav.map((item) => (
            <button
              className={view === item.id ? "active" : ""}
              key={item.id}
              onClick={() => setView(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        {view === "register" && (
          <RegisterAccountPicker
            accounts={state.accounts}
            selectedId={registerAccountId}
            query={registerAccountQuery}
            onQuery={setRegisterAccountQuery}
            onSelect={setRegisterAccountId}
          />
        )}
        <button className="ghost" onClick={logout}>Log out</button>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <h1>{visibleNav.find((item) => item.id === view)?.label}</h1>
            <p>{roleAtLeast(session, "manager") ? "Manager controls enabled" : "Cashier checkout mode"}</p>
          </div>
          <Metrics state={state} />
        </header>

        {error && <div className="alert error">{error}</div>}
        {toast && <div className="alert success">{toast}</div>}

        {view === "register" && <Register state={state} run={run} currentUser={session} accountId={registerAccountId} />}
        {view === "accounts" && roleAtLeast(session, "manager") && <Accounts state={state} run={run} />}
        {view === "inventory" && roleAtLeast(session, "manager") && <Inventory state={state} run={run} />}
        {view === "reports" && roleAtLeast(session, "manager") && <DailyReports reports={state.dailyReports || []} timeZone={state.reportTimeZone} />}
        {view === "ledger" && roleAtLeast(session, "manager") && <Ledger transactions={state.transactions} run={run} />}
        {view === "refunds" && roleAtLeast(session, "manager") && <Refunds state={state} run={run} />}
        {view === "workers" && roleAtLeast(session, "manager") && <Workers run={run} />}
        {view === "data" && roleAtLeast(session, "manager") && <DataTools run={run} />}
      </main>
    </div>
  );
}

function Shell({ status }) {
  return <div className="centered">{status}</div>;
}

function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ email, password })
      });
      await onLogin();
    } catch (err) {
      setError(err.message || "Login failed");
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <span className="login-mark">C</span>
        <h1>Canteen POS</h1>
        <p>Sign in with your worker account.</p>
        <label>Email<input value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <button>Log in</button>
        {error && <strong className="form-error">{error}</strong>}
      </form>
    </div>
  );
}

function Metrics({ state }) {
  const balances = state.accounts.reduce((sum, account) => sum + Number(account.balance || 0), 0);
  const inventory = state.items.reduce((sum, item) => sum + Number(item.price || 0) * Number(item.quantity || 0), 0);
  const low = state.items.filter((item) => item.active && item.quantity <= item.lowStockAt).length;
  return (
    <section className="metrics">
      <article><span>Balances</span><strong>{formatMoney(balances)}</strong></article>
      <article><span>Inventory</span><strong>{formatMoney(inventory)}</strong></article>
      <article><span>Accounts</span><strong>{state.accounts.length}</strong></article>
      <article><span>Low stock</span><strong>{low}</strong></article>
    </section>
  );
}

function RegisterAccountPicker({ accounts, selectedId, query, onQuery, onSelect }) {
  const normalizedQuery = query.trim().toLowerCase();
  const visibleAccounts = normalizedQuery
    ? accounts.filter((item) => `${item.name} ${item.note || ""} ${formatMoney(item.balance)}`.toLowerCase().includes(normalizedQuery))
    : accounts;

  return (
    <section className="sidebar-register">
      <div className="sidebar-section-head">
        <strong>Accounts</strong>
        <small>{visibleAccounts.length} of {accounts.length}</small>
      </div>
      <input
        placeholder="Search accounts"
        value={query}
        onChange={(e) => onQuery(e.target.value)}
      />
      <div className="sidebar-account-list">
        {visibleAccounts.map((item) => (
          <button
            className={item.id === selectedId ? "sidebar-account selected" : "sidebar-account"}
            key={item.id}
            onClick={() => onSelect(item.id)}
          >
            <span>
              <strong>{item.name}</strong>
              {item.note && <small>{item.note}</small>}
            </span>
            <em>{formatMoney(item.balance)}</em>
          </button>
        ))}
        {!visibleAccounts.length && <em className="sidebar-empty">No accounts found.</em>}
      </div>
    </section>
  );
}

function Register({ state, run, currentUser, accountId }) {
  const [productQuery, setProductQuery] = useState("");
  const [cart, setCart] = useState({});
  const [note, setNote] = useState("");
  const [allowNegative, setAllowNegative] = useState(false);
  const canAllowNegative = roleAtLeast(currentUser, "manager");

  const account = state.accounts.find((item) => item.id === accountId);
  const products = state.items.filter((item) => item.active && `${item.name} ${item.category}`.toLowerCase().includes(productQuery.toLowerCase()));
  const lines = Object.entries(cart).map(([itemId, quantity]) => {
    const item = state.items.find((candidate) => candidate.id === itemId);
    return item ? { item, quantity } : null;
  }).filter(Boolean);
  const total = lines.reduce((sum, line) => sum + line.item.price * line.quantity, 0);

  function setLineQuantity(item, quantity) {
    setCart((next) => {
      const nextQuantity = Math.min(Math.max(quantity, 0), item.quantity);
      if (!nextQuantity) {
        const rest = { ...next };
        delete rest[item.id];
        return rest;
      }
      return { ...next, [item.id]: nextQuantity };
    });
  }

  function add(item) {
    setLineQuantity(item, (cart[item.id] || 0) + 1);
  }

  function subtract(item) {
    setLineQuantity(item, (cart[item.id] || 0) - 1);
  }

  function remove(item) {
    setLineQuantity(item, 0);
  }

  async function checkout() {
    const saved = await run(
      () => api("/api/sales", {
        method: "POST",
        body: JSON.stringify({
          accountId,
          note,
          allowNegative: canAllowNegative && allowNegative,
          lines: lines.map((line) => ({ itemId: line.item.id, quantity: line.quantity }))
        })
      }),
      "Sale complete"
    );
    if (saved) {
      setCart({});
      setNote("");
      setAllowNegative(false);
    }
  }

  return (
    <div className="register-grid">
      <section className="panel">
        <div className="panel-head">
          <h2>Catalog</h2>
          <input placeholder="Search products" value={productQuery} onChange={(e) => setProductQuery(e.target.value)} />
        </div>
        <div className="product-grid">
          {products.map((item) => (
            <button className="product" disabled={item.quantity <= 0} key={item.id} onClick={() => add(item)}>
              <small>{item.category}</small>
              <strong>{item.name}</strong>
              <span>{formatMoney(item.price)} · {item.quantity} left{cart[item.id] ? ` · ${cart[item.id]} in ticket` : ""}</span>
            </button>
          ))}
        </div>
      </section>
      <section className="panel ticket">
        <h2>Ticket</h2>
        <p>{account ? `${account.name} after sale: ${formatMoney(account.balance - total)}` : "Select account"}</p>
        <div className="list">
          {lines.length ? lines.map((line) => (
            <div className="ticket-row" key={line.item.id}>
              <div className="ticket-item">
                <strong>{line.item.name}</strong>
                <small>{formatMoney(line.item.price)} each · {line.item.quantity} available</small>
              </div>
              <div className="quantity-controls">
                <button aria-label={`Subtract ${line.item.name}`} onClick={() => subtract(line.item)}>-</button>
                <strong>{line.quantity}</strong>
                <button aria-label={`Add ${line.item.name}`} disabled={line.quantity >= line.item.quantity} onClick={() => add(line.item)}>+</button>
              </div>
              <strong>{formatMoney(line.quantity * line.item.price)}</strong>
              <button className="ghost-control" onClick={() => remove(line.item)}>Remove</button>
            </div>
          )) : <em>Empty ticket</em>}
        </div>
        <div className="total"><span>Total</span><strong>{formatMoney(total)}</strong></div>
        {canAllowNegative && (
          <label className="check">
            <input type="checkbox" checked={allowNegative} onChange={(e) => setAllowNegative(e.target.checked)} />
            Allow negative balance
          </label>
        )}
        <textarea placeholder="Sale note" value={note} onChange={(e) => setNote(e.target.value)} />
        <button disabled={!accountId || !lines.length} onClick={checkout}>Complete sale</button>
      </section>
    </div>
  );
}

function Accounts({ state, run }) {
  const [selected, setSelected] = useState(state.accounts[0]?.id || "");
  const [accountSearch, setAccountSearch] = useState("");
  const account = state.accounts.find((item) => item.id === selected);
  const normalizedAccountSearch = accountSearch.trim().toLowerCase();
  const visibleAccounts = normalizedAccountSearch
    ? state.accounts.filter((item) => `${item.name} ${item.note || ""} ${formatMoney(item.balance)}`.toLowerCase().includes(normalizedAccountSearch))
    : state.accounts;

  useEffect(() => {
    if (!state.accounts.length) {
      setSelected("");
      return;
    }
    if (!state.accounts.some((item) => item.id === selected)) {
      setSelected(state.accounts[0].id);
    }
  }, [selected, state.accounts]);

  return (
    <div className="admin-grid">
      <section className="panel">
        <div className="panel-stack-head">
          <h2>Accounts</h2>
          <input
            placeholder="Search accounts"
            value={accountSearch}
            onChange={(e) => setAccountSearch(e.target.value)}
          />
          <small>Showing {visibleAccounts.length} of {state.accounts.length} accounts.</small>
        </div>
        <CreateAccount run={run} />
        <div className="list">
          {visibleAccounts.map((item) => (
            <button className={item.id === selected ? "selected row" : "row"} key={item.id} onClick={() => setSelected(item.id)}>
              <span className="row-main">
                <strong>{item.name}</strong>
                {item.note && <small>{item.note}</small>}
              </span>
              <strong>{formatMoney(item.balance)}</strong>
            </button>
          ))}
          {!visibleAccounts.length && <em className="empty-state">No accounts found.</em>}
        </div>
      </section>
      <section className="panel">
        <h2>Account controls</h2>
        {account ? (
          <div className="account-controls">
            <AccountDetails account={account} run={run} />
            <BalanceForms account={account} run={run} />
          </div>
        ) : <p>Select an account.</p>}
      </section>
    </div>
  );
}

function CreateAccount({ run }) {
  const [name, setName] = useState("");
  const [openingBalance, setOpeningBalance] = useState(0);
  const [note, setNote] = useState("");
  const [bulkNames, setBulkNames] = useState("");
  const [bulkOpeningBalance, setBulkOpeningBalance] = useState(0);
  const [bulkResult, setBulkResult] = useState("");
  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api("/api/accounts", { method: "POST", body: JSON.stringify({ name, openingBalance, note }) }),
      "Account created"
    );
    if (saved) {
      setName("");
      setOpeningBalance(0);
      setNote("");
    }
  }

  async function submitBulk(e) {
    e.preventDefault();
    const names = bulkNames.split(/\r?\n|,/).map((entry) => entry.trim()).filter(Boolean);
    const saved = await run(
      async () => {
        const response = await api("/api/accounts/bulk", {
          method: "POST",
          body: JSON.stringify({ names, openingBalance: bulkOpeningBalance })
        });
        setBulkResult(`${response.created.length} created${response.skipped.length ? `, ${response.skipped.length} skipped` : ""}`);
      },
      "Accounts imported"
    );
    if (saved) {
      setBulkNames("");
      setBulkOpeningBalance(0);
    }
  }

  return (
    <div className="stack">
      <form className="stack" onSubmit={submit}>
        <input placeholder="New account name" value={name} onChange={(e) => setName(e.target.value)} />
        <input type="number" step="0.01" value={openingBalance} onChange={(e) => setOpeningBalance(e.target.value)} />
        <textarea placeholder="Account note" value={note} onChange={(e) => setNote(e.target.value)} />
        <button>Create account</button>
      </form>
      <form className="bulk-import" onSubmit={submitBulk}>
        <h3>Import accounts</h3>
        <textarea
          placeholder={"Paste names, one per line\nCamper One\nCamper Two"}
          value={bulkNames}
          onChange={(e) => setBulkNames(e.target.value)}
        />
        <input
          type="number"
          step="0.01"
          value={bulkOpeningBalance}
          onChange={(e) => setBulkOpeningBalance(e.target.value)}
          aria-label="Opening balance for imported accounts"
        />
        <button disabled={!bulkNames.trim()}>Import list</button>
        {bulkResult && <small>{bulkResult}</small>}
      </form>
    </div>
  );
}

function AccountDetails({ account, run }) {
  const [draft, setDraft] = useState({ name: account.name, note: account.note || "" });

  useEffect(() => {
    setDraft({ name: account.name, note: account.note || "" });
  }, [account.id, account.name, account.note]);

  async function submit(e) {
    e.preventDefault();
    await run(
      () => api(`/api/accounts/${account.id}`, { method: "PATCH", body: JSON.stringify(draft) }),
      "Account saved"
    );
  }

  const unchanged = draft.name === account.name && draft.note === (account.note || "");

  return (
    <form className="card account-details" onSubmit={submit}>
      <h3>Account details</h3>
      <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      <textarea value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} placeholder="Account note" />
      <button disabled={unchanged}>Save account</button>
    </form>
  );
}

function BalanceForms({ account, run }) {
  return (
    <div className="cards3">
      <FundsForm label="Add funds" endpoint={`/api/accounts/${account.id}/funds/add`} run={run} />
      <FundsForm label="Subtract funds" endpoint={`/api/accounts/${account.id}/funds/subtract`} run={run} />
      <SetBalance account={account} run={run} />
    </div>
  );
}

function FundsForm({ label, endpoint, run }) {
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api(endpoint, { method: "POST", body: JSON.stringify({ amount, note }) }),
      "Balance updated"
    );
    if (saved) {
      setAmount("");
      setNote("");
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h3>{label}</h3>
      <input type="number" step="0.01" placeholder="Amount" value={amount} onChange={(e) => setAmount(e.target.value)} />
      <input placeholder="Reason required" value={note} onChange={(e) => setNote(e.target.value)} required />
      <button>{label}</button>
    </form>
  );
}

function SetBalance({ account, run }) {
  const [balance, setBalance] = useState(account.balance);
  const [note, setNote] = useState("");
  useEffect(() => {
    setBalance(account.balance);
    setNote("");
  }, [account.id, account.balance]);

  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api(`/api/accounts/${account.id}/balance/set`, { method: "POST", body: JSON.stringify({ balance, note }) }),
      "Balance set"
    );
    if (saved) setNote("");
  }

  return (
    <form className="card" onSubmit={submit}>
      <h3>Set balance</h3>
      <input type="number" step="0.01" value={balance} onChange={(e) => setBalance(e.target.value)} />
      <input placeholder="Reason required" value={note} onChange={(e) => setNote(e.target.value)} required />
      <button>Set balance</button>
    </form>
  );
}

function Inventory({ state, run }) {
  const [itemQuery, setItemQuery] = useState("");
  const normalizedItemQuery = itemQuery.trim().toLowerCase();
  const items = normalizedItemQuery
    ? state.items.filter((item) => (
        `${item.name} ${item.category} ${formatMoney(item.price)} ${item.quantity}`.toLowerCase().includes(normalizedItemQuery)
      ))
    : state.items;

  return (
    <section className="panel inventory-panel">
      <h2>Inventory</h2>
      <section className="inventory-section">
        <div className="section-head">
          <h3>Add product</h3>
          <p>Create a new item for the register.</p>
        </div>
        <ItemForm run={run} />
      </section>
      <section className="inventory-section">
        <div className="inventory-toolbar">
          <div className="section-head">
            <h3>Edit products</h3>
            <p>Showing {items.length} of {state.items.length} products.</p>
          </div>
          <input
            placeholder="Search products to edit"
            value={itemQuery}
            onChange={(e) => setItemQuery(e.target.value)}
          />
        </div>
        <div className="table">
          <div className="table-row table-header" aria-hidden="true">
            {itemFields.map((field) => <span key={field.key}>{field.label}</span>)}
            <span>Actions</span>
          </div>
          {items.map((item) => <ItemRow key={item.id} item={item} run={run} />)}
          {!items.length && <em className="empty-state">No products found.</em>}
        </div>
      </section>
    </section>
  );
}

function ItemForm({ run }) {
  const [item, setItem] = useState({ name: "", category: "General", price: 0, quantity: 0, lowStockAt: 5, active: true });
  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api("/api/items", { method: "POST", body: JSON.stringify(item) }),
      "Product added"
    );
    if (saved) setItem({ name: "", category: "General", price: 0, quantity: 0, lowStockAt: 5, active: true });
  }

  return (
    <form className="inventory-form" onSubmit={submit}>
      {itemFields.map((field) => (
        <label className="field" key={field.key}>
          <span>{field.label}</span>
          <input
            placeholder={field.placeholder}
            value={item[field.key]}
            type={field.type}
            step={field.step}
            min={field.min}
            onChange={(e) => setItem({ ...item, [field.key]: e.target.value })}
          />
        </label>
      ))}
      <button>Add product</button>
    </form>
  );
}

function ItemRow({ item, run }) {
  const [draft, setDraft] = useState(item);
  useEffect(() => {
    setDraft(item);
  }, [item]);

  return (
    <div className={item.active ? "table-row" : "table-row muted"}>
      {itemFields.map((field) => (
        <label className="field" key={field.key}>
          <span>{field.label}</span>
          <input
            value={draft[field.key]}
            type={field.type}
            step={field.step}
            min={field.min}
            onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
          />
        </label>
      ))}
      <div className="row-actions">
        <button onClick={() => run(() => api(`/api/items/${item.id}`, { method: "PATCH", body: JSON.stringify({ ...draft, active: true }) }), "Product saved")}>Save</button>
        <button className="danger" onClick={() => run(() => api(`/api/items/${item.id}`, { method: "DELETE" }), "Product hidden")}>Hide</button>
      </div>
    </div>
  );
}

function formatReportDay(value) {
  if (!value) return "";
  return new Date(value).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric"
  });
}

function formatReportTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit"
  });
}

function DailyReports({ reports, timeZone }) {
  return (
    <section className="panel reports-panel">
      <div className="panel-head">
        <div>
          <h2>Daily reports</h2>
          <p>{timeZone || "Local time"} · 12:00 AM to 12:00 AM</p>
        </div>
      </div>
      <div className="report-grid">
        {reports.map((report) => (
          <article className="report-card" key={report.date}>
            <div>
              <h3>{formatReportDay(report.startsAt)}</h3>
              <small>{formatReportTime(report.startsAt)} to {formatReportTime(report.endsAt)}</small>
            </div>
            <strong>{formatMoney(report.salesTotal)}</strong>
            <dl>
              <div><dt>Purchases</dt><dd>{report.purchaseCount}</dd></div>
              <div><dt>Items</dt><dd>{report.itemsSold}</dd></div>
              <div><dt>Funds in</dt><dd>{formatMoney(report.fundsAdded)}</dd></div>
              <div><dt>Funds out</dt><dd>{formatMoney(report.fundsSubtracted)}</dd></div>
              <div><dt>Undos</dt><dd>{report.undoCount}</dd></div>
              <div><dt>Entries</dt><dd>{report.transactionCount}</dd></div>
            </dl>
          </article>
        ))}
        {!reports.length && <em className="empty-state">No reports found.</em>}
      </div>
    </section>
  );
}

function Ledger({ transactions, run }) {
  const [ledgerQuery, setLedgerQuery] = useState("");
  const normalizedLedgerQuery = ledgerQuery.trim().toLowerCase();
  const visibleTransactions = normalizedLedgerQuery
    ? transactions.filter((txn) => {
        const itemText = (txn.items || [])
          .map((item) => `${item.name} ${item.quantity} ${formatMoney(item.unitPrice)} ${formatMoney(item.lineTotal)}`)
          .join(" ");
        return [
          txn.type,
          txn.accountName,
          txn.actorName,
          txn.actorRole,
          txn.note,
          formatDate(txn.date),
          new Date(txn.date).toLocaleDateString(),
          formatMoney(txn.amount),
          itemText
        ].join(" ").toLowerCase().includes(normalizedLedgerQuery);
    })
    : transactions;

  async function undo(txn) {
    if (!confirm(`Undo ${txn.type} for ${formatMoney(txn.amount)}?`)) return;
    await run(
      () => api(`/api/transactions/${txn.id}/undo`, {
        method: "POST",
        body: JSON.stringify({ note: `Undo ${txn.type}` })
      }),
      "Transaction undone"
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Audit ledger</h2>
          <p>Showing {visibleTransactions.length} of {transactions.length} entries.</p>
        </div>
        <input
          placeholder="Search date, account, item"
          value={ledgerQuery}
          onChange={(e) => setLedgerQuery(e.target.value)}
        />
      </div>
      <div className="list">
        {visibleTransactions.map((txn) => (
          <div className={txn.undone ? "ledger-row muted" : "ledger-row"} key={txn.id}>
            <div>
              <strong>{txn.type}</strong>
              <small>{formatDate(txn.date)} · {txn.actorName} ({txn.actorRole}){txn.accountName ? ` · ${txn.accountName}` : ""}</small>
              <p>{txn.note}</p>
              {txn.items?.length > 0 && (
                <div className="ledger-items">
                  {txn.items.map((item) => (
                    <div className="ledger-item" key={`${txn.id}-${item.itemId || item.name}`}>
                      <span>{item.quantity} x {item.name}</span>
                      <small>{formatMoney(item.unitPrice)} each</small>
                      <strong>{formatMoney(item.lineTotal)}</strong>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="ledger-actions">
              <strong>{formatMoney(txn.amount)}</strong>
              {txn.undone && <span className="status-pill">Undone</span>}
              {txn.canUndo && <button className="ghost-control" onClick={() => undo(txn)}>Undo</button>}
            </div>
          </div>
        ))}
        {!visibleTransactions.length && <em className="empty-state">No ledger entries found.</em>}
      </div>
    </section>
  );
}

function latestRefundsByAccount(transactions) {
  const map = new Map();
  for (const txn of transactions) {
    if (txn.type !== "refund" || txn.undone || !txn.accountId) continue;
    const existing = map.get(txn.accountId);
    if (!existing || new Date(txn.date) > new Date(existing.date)) {
      map.set(txn.accountId, txn);
    }
  }
  return map;
}

function Refunds({ state, run }) {
  const [query, setQuery] = useState("");
  const refunded = latestRefundsByAccount(state.transactions || []);
  const candidates = state.accounts.filter((account) => account.balance > 0 || refunded.has(account.id));
  const normalizedQuery = query.trim().toLowerCase();
  const visible = normalizedQuery
    ? candidates.filter((account) => `${account.name} ${account.note || ""}`.toLowerCase().includes(normalizedQuery))
    : candidates;
  const sorted = [...visible].sort((a, b) => {
    const aDone = a.balance <= 0;
    const bDone = b.balance <= 0;
    if (aDone !== bDone) return aDone ? 1 : -1;
    return b.balance - a.balance;
  });
  const outstanding = candidates.filter((account) => account.balance > 0);
  const outstandingTotal = outstanding.reduce((sum, account) => sum + Number(account.balance || 0), 0);

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>End of season refunds</h2>
          <p>{outstanding.length} account{outstanding.length === 1 ? "" : "s"} still owed {formatMoney(outstandingTotal)}.</p>
        </div>
        <input placeholder="Search accounts" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div className="list">
        {sorted.map((account) => (
          <RefundRow key={account.id} account={account} refundTxn={refunded.get(account.id)} run={run} />
        ))}
        {!sorted.length && <em className="empty-state">No positive balances to refund.</em>}
      </div>
    </section>
  );
}

const refundMethods = [
  { value: "cash", label: "Cash" },
  { value: "check", label: "Check" },
  { value: "venmo", label: "Venmo" },
  { value: "zelle", label: "Zelle" },
  { value: "other", label: "Other" }
];

function RefundRow({ account, refundTxn, run }) {
  const [open, setOpen] = useState(false);
  const [method, setMethod] = useState("cash");
  const [note, setNote] = useState("");
  const owesRefund = account.balance > 0;

  useEffect(() => {
    if (!owesRefund) setOpen(false);
  }, [owesRefund]);

  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api(`/api/accounts/${account.id}/refund`, { method: "POST", body: JSON.stringify({ method, note }) }),
      "Refund recorded"
    );
    if (saved) {
      setOpen(false);
      setNote("");
    }
  }

  return (
    <div className="refund-row">
      <div className="row-main">
        <strong>{account.name}</strong>
        {account.note && <small>{account.note}</small>}
      </div>
      <strong>{formatMoney(account.balance)}</strong>
      {!owesRefund && (
        <span className="status-pill success">
          {refundTxn
            ? `Refunded (${refundTxn.details?.method || "?"}) ${formatDate(refundTxn.date)}`
            : "$0 balance"}
        </span>
      )}
      {owesRefund && !open && <button onClick={() => setOpen(true)}>Mark refunded</button>}
      {owesRefund && open && (
        <form className="refund-form" onSubmit={submit}>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {refundMethods.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          <input placeholder="Note required" value={note} onChange={(e) => setNote(e.target.value)} required />
          <div className="row-actions">
            <button type="submit">Confirm</button>
            <button type="button" className="ghost-control" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </form>
      )}
    </div>
  );
}

function Workers({ run }) {
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ email: "", name: "", password: "", role: "cashier" });

  async function load() {
    const data = await api("/api/users");
    setUsers(data.users);
  }

  useEffect(() => { load(); }, []);

  async function submit(e) {
    e.preventDefault();
    const saved = await run(
      () => api("/api/users", { method: "POST", body: JSON.stringify(form) }),
      "Worker created"
    );
    if (saved) {
      setForm({ email: "", name: "", password: "", role: "cashier" });
      await load();
    }
  }

  return (
    <section className="panel">
      <h2>Workers</h2>
      <form className="inventory-form" onSubmit={submit}>
        <input placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input placeholder="Password" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          <option value="cashier">Cashier</option>
          <option value="manager">Manager</option>
        </select>
        <button>Create worker</button>
      </form>
      <div className="list">
        {users.map((user) => (
          <WorkerRow key={user.id} user={user} run={run} onChange={load} />
        ))}
      </div>
    </section>
  );
}

function WorkerRow({ user, run, onChange }) {
  const [role, setRole] = useState(displayRole(user.role));

  useEffect(() => {
    setRole(displayRole(user.role));
  }, [user.role]);

  async function saveRole() {
    const saved = await run(
      () => api(`/api/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ role }) }),
      "Worker updated"
    );
    if (saved) await onChange();
  }

  async function toggleActive() {
    const saved = await run(
      () => api(`/api/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ active: !user.active }) }),
      user.active ? "Worker deactivated" : "Worker reactivated"
    );
    if (saved) await onChange();
  }

  return (
    <div className={user.active ? "worker-row" : "worker-row muted"}>
      <div>
        <strong>{user.name}</strong>
        <small>{user.email}</small>
      </div>
      <select value={role} onChange={(e) => setRole(e.target.value)}>
        <option value="cashier">Cashier</option>
        <option value="manager">Manager</option>
      </select>
      <button disabled={role === displayRole(user.role)} onClick={saveRole}>Save role</button>
      <button className={user.active ? "danger" : ""} onClick={toggleActive}>
        {user.active ? "Deactivate" : "Reactivate"}
      </button>
    </div>
  );
}

function DataTools({ run }) {
  async function backup() {
    const data = await api("/api/backups");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "canteen-backup.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  async function restoreFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    await run(async () => {
      let parsed;
      try {
        parsed = JSON.parse(await file.text());
      } catch {
        throw new Error("Backup file is not valid JSON");
      }
      return api("/api/backups", {
        method: "POST",
        body: JSON.stringify(parsed)
      });
    }, "Backup restored");
  }

  return (
    <section className="panel">
      <h2>Data</h2>
      <div className="cards3">
        <button onClick={backup}>Download backup</button>
        <label className="buttonlike">
          Restore backup
          <input hidden type="file" accept="application/json" onChange={restoreFile} />
        </label>
        <button className="danger" onClick={() => {
          if (confirm("Clear all accounts, items, and ledger data?")) {
            run(() => api("/api/backups", { method: "DELETE" }), "Database cleared");
          }
        }}>Clear database</button>
      </div>
    </section>
  );
}
