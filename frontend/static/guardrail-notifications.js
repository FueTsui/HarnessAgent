/* Shared administrator inbox; server checks current role on every request. */
(() => {
  let busy = false;
  let panel;
  async function refresh() {
    if (busy || document.hidden || !Auth.isAdmin() || !Auth.canModule("guardrails")) return;
    busy = true;
    try {
      const data = await api("/api/v1/guardrails/reviews");
      if (!panel) {
        panel = document.createElement("details");
        panel.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:1000;background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:12px;max-width:420px;max-height:60vh;overflow:auto;box-shadow:0 4px 20px color-mix(in srgb, var(--line-strong) 35%, transparent)";
        document.body.appendChild(panel);
      }
      panel.hidden = !data.items.length;
      panel.replaceChildren();
      const title = document.createElement("summary");
      title.textContent = `护栏待审批（${data.items.length}）`;
      panel.appendChild(title);
      for (const row of data.items) {
        const card = document.createElement("div");
        const text = document.createElement("p");
        const rules = row.summary.matches || row.summary.matched_rules || [];
        text.textContent = `用户 #${row.user_id} · ${row.summary.point || row.summary.tool || "工具调用"}：${rules.map(r => r.policy_name || r.name || r.detector).join("、")}。批准仅放行本次检查；5分钟内有效。`;
        card.appendChild(text);
        for (const [decision, label] of [["approved", "批准本次"], ["rejected", "拒绝"]]) {
          const button = document.createElement("button");
          button.className = "btn ghost";
          button.textContent = label;
          button.onclick = async () => {
            card.querySelectorAll("button").forEach(b => b.disabled = true);
            try {
              await api(`/api/v1/guardrails/reviews/${row.id}/decision`, {method: "POST", json: {decision}});
              await refresh();
            } catch (error) {
              text.textContent = error.message;
              card.querySelectorAll("button").forEach(b => b.disabled = false);
            }
          };
          card.appendChild(button);
        }
        panel.appendChild(card);
      }
    } catch (_) {
      if (panel) panel.hidden = true;
    } finally { busy = false; }
  }
  document.addEventListener("visibilitychange", refresh);
  refresh();
  setInterval(refresh, 5000);
})();
