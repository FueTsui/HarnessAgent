function loginDestination(search) {
  const next = new URLSearchParams(search).get("next");
  return ["/admin", "/"].includes(next) ? next : "/";
}

const loginNext = loginDestination(location.search);
applyBranding();
hydrateIcons();
document.getElementById("login-theme").onclick = () => Theme.toggle();

Auth.readSession().then(user => {
  if (user && !Auth.isGuest()) location.href = loginNext;
}).catch(() => {});

const errorEl = document.getElementById("error");
let submittingLogin = false;
async function doLogin() {
  if (submittingLogin) return;
  errorEl.textContent = "";
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  if (!username || !password) {
    errorEl.textContent = "请输入用户名和密码";
    return;
  }
  submittingLogin = true;
  document.getElementById("login-btn").disabled = true;
  try {
    const data = await api("/api/v1/auth/login", {
      method: "POST", json: {username, password},
    });
    Auth.save(data);
    location.href = loginNext;
  } catch (error) {
    errorEl.textContent = error.message;
  } finally {
    submittingLogin = false;
    document.getElementById("login-btn").disabled = false;
  }
}

document.getElementById("login-btn").onclick = doLogin;
document.addEventListener("keydown", event => {
  if (event.key === "Enter" && ["username", "password"].includes(event.target.id)) {
    event.preventDefault();
    doLogin();
  }
});
