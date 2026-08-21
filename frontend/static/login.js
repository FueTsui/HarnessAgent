applyBranding();
hydrateIcons();
document.getElementById("login-theme").onclick = () => Theme.toggle();

fetch("/api/v1/auth/me").then(response => {
  if (response.ok) location.href = "/";
}).catch(() => {});

const errorEl = document.getElementById("error");
async function doLogin() {
  errorEl.textContent = "";
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  if (!username || !password) {
    errorEl.textContent = "请输入用户名和密码";
    return;
  }
  try {
    const data = await api("/api/v1/auth/login", {
      method: "POST", json: {username, password},
    });
    Auth.save(data);
    location.href = "/";
  } catch (error) {
    errorEl.textContent = error.message;
  }
}

document.getElementById("login-btn").onclick = doLogin;
document.addEventListener("keydown", event => {
  if (event.key === "Enter") doLogin();
});
