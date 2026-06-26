export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    },
    ...options
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text };
    }
  }
  if (!response.ok) {
    const error = new Error(data?.detail || data?.error || "Request failed");
    error.status = response.status;
    throw error;
  }
  return data;
}

export function roleAtLeast(user, role) {
  const levels = { cashier: 1, manager: 2, owner: 2 };
  return levels[user?.role] >= levels[role];
}

export const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD"
});

export function formatMoney(value) {
  return money.format(Number(value) || 0);
}

export function formatDate(value) {
  if (!value) return "";
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  });
}
