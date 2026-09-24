using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("Harness Agent")]
[assembly: AssemblyDescription("Harness Agent 本地独立运行控制台")]
[assembly: AssemblyCompany("Harness Agent")]
[assembly: AssemblyProduct("Harness Agent")]
[assembly: AssemblyCopyright("Harness Agent")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace HarnessAgentDesktop
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HarnessAgent");
            bool openBrowser = true;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--home" && i + 1 < args.Length) home = args[++i];
                else if (args[i] == "--no-browser") openBrowser = false;
                else
                {
                    MessageBox.Show("用法：HarnessAgent.exe [--home 数据目录] [--no-browser]\n\n首次启动会创建独立的本地数据和管理员账户。", "Harness Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
            }
            try
            {
                home = Path.GetFullPath(home);
                Directory.CreateDirectory(home);
                string key;
                using (SHA256 sha = SHA256.Create())
                    key = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(home.TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant()))).Replace("-", "").Substring(0, 32);
                bool created;
                using (Mutex mutex = new Mutex(true, "Local\\HarnessAgent-" + key, out created))
                {
                    if (!created)
                    {
                        ReadyStatus ready = LocalService.ReadStatus(home);
                        if (ready != null && LocalService.IsPackagedPython(ready.Pid) && LocalService.IsHealthy(ready.Url))
                        {
                            if (MessageBox.Show("此数据目录的 Harness Agent 已在运行。\n是否打开 Agent 工作区？", "Harness Agent", MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                                LocalService.Open(ready.Url);
                        }
                        else MessageBox.Show("此数据目录的 Harness Agent 控制台已打开，可能正在启动或停止。\n请使用现有控制台。", "Harness Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return;
                    }
                    try { Application.Run(new MainForm(home, openBrowser)); }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show("无法打开本地 Agent：\n" + ex.Message, "Harness Agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    internal sealed class ReadyStatus
    {
        public string Url;
        public int Pid;
    }

    internal static class LocalService
    {
        public static string PackageRoot { get { return AppDomain.CurrentDomain.BaseDirectory; } }
        public static string PythonPath { get { return Path.Combine(PackageRoot, "runtime", "python.exe"); } }

        // CommandLineToArgvW-compatible quoting, including paths ending in backslashes.
        public static string Quote(string value)
        {
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char ch in value)
            {
                if (ch == '\\') { slashes++; continue; }
                if (ch == '"') result.Append('\\', slashes * 2 + 1).Append(ch);
                else result.Append('\\', slashes).Append(ch);
                slashes = 0;
            }
            return result.Append('\\', slashes * 2).Append('"').ToString();
        }

        public static ReadyStatus ReadStatus(string home)
        {
            try
            {
                string path = Path.Combine(home, "runtime.json");
                string json;
                using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    if (stream.Length > 65536) return null;
                    using (StreamReader reader = new StreamReader(stream, Encoding.UTF8)) json = reader.ReadToEnd();
                }
                Dictionary<string, object> value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
                object state, url, pid;
                if (!value.TryGetValue("state", out state) || Convert.ToString(state) != "ready" ||
                    !value.TryGetValue("url", out url) || !value.TryGetValue("pid", out pid)) return null;
                Uri uri;
                if (!Uri.TryCreate(Convert.ToString(url), UriKind.Absolute, out uri) || uri.Scheme != "http" ||
                    (uri.Host != "127.0.0.1" && uri.Host != "localhost" && uri.Host != "[::1]") ||
                    !String.IsNullOrEmpty(uri.UserInfo) || uri.Port < 1 || uri.AbsolutePath != "/" ||
                    !String.IsNullOrEmpty(uri.Query) || !String.IsNullOrEmpty(uri.Fragment)) return null;
                int processId = Convert.ToInt32(pid);
                if (processId <= 0) return null;
                return new ReadyStatus { Url = uri.AbsoluteUri, Pid = processId };
            }
            catch { return null; }
        }

        public static bool IsHealthy(string url)
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(new Uri(new Uri(url), "healthz"));
                request.Timeout = 1200;
                request.ReadWriteTimeout = 1200;
                request.AllowAutoRedirect = false;
                request.Proxy = null;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    return response.StatusCode == HttpStatusCode.OK;
            }
            catch { return false; }
        }

        public static bool IsPackagedPython(int pid)
        {
            try
            {
                using (Process process = Process.GetProcessById(pid))
                    return !process.HasExited && String.Equals(Path.GetFullPath(process.MainModule.FileName), Path.GetFullPath(PythonPath), StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        public static void Open(string path)
        {
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception ex) { MessageBox.Show("无法打开：\n" + path + "\n\n" + ex.Message, "Harness Agent", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        }
    }

    internal enum ServiceState { Starting, Running, Stopping, Stopped, Failed }

    internal sealed class MainForm : Form
    {
        private readonly string home;
        private readonly bool autoOpen;
        private readonly object logLock = new object();
        private readonly System.Windows.Forms.Timer timer;
        private Label statusTitle, statusDetail, address, footer;
        private Panel statusDot;
        private Button openButton, toggleButton, restartButton, credentialsButton, logsButton;
        private Process child;
        private OwnedProcessJob childJob;
        private StreamWriter logWriter;
        private string logPath, currentUrl;
        private ServiceState state = ServiceState.Stopped;
        private DateTime launchTime;
        private bool browserOpened, exiting, stopPending;
        private int checkPending;
        private int unhealthyChecks;
        private readonly Color ink = Color.FromArgb(230, 234, 243);
        private readonly Color muted = Color.FromArgb(151, 162, 183);
        private readonly Color accent = Color.FromArgb(148, 126, 247);

        public MainForm(string dataHome, bool shouldOpenBrowser)
        {
            home = dataHome;
            autoOpen = shouldOpenBrowser;
            BuildInterface();
            timer = new System.Windows.Forms.Timer();
            timer.Interval = 1100;
            timer.Tick += delegate { PollService(); };
            timer.Start();
            Shown += delegate { StartService(); };
            FormClosing += HandleClosing;
        }

        private Label TextLabel(string text, float size, FontStyle style, Color color, int x, int y, int width, int height)
        {
            return new Label { Text = text, Font = new Font("Microsoft YaHei UI", size, style), ForeColor = color, BackColor = Color.Transparent,
                Location = new Point(x, y), Size = new Size(width, height), AutoEllipsis = true };
        }

        private Button ActionButton(string text, int x, int y, int width, bool primary)
        {
            Button button = new Button { Text = text, Location = new Point(x, y), Size = new Size(width, 42),
                Font = new Font("Microsoft YaHei UI", 10F), Cursor = Cursors.Hand, FlatStyle = FlatStyle.Flat,
                BackColor = primary ? accent : Color.FromArgb(37, 43, 58), ForeColor = primary ? Color.FromArgb(22, 19, 39) : ink,
                UseVisualStyleBackColor = false };
            button.FlatAppearance.BorderSize = primary ? 0 : 1;
            button.FlatAppearance.BorderColor = Color.FromArgb(67, 76, 99);
            button.FlatAppearance.MouseOverBackColor = primary ? Color.FromArgb(167, 148, 255) : Color.FromArgb(52, 60, 79);
            return button;
        }

        private void BuildInterface()
        {
            Text = "Harness Agent · 本地控制台";
            ClientSize = new Size(740, 470);
            MinimumSize = new Size(756, 509);
            MaximumSize = new Size(756, 509);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            BackColor = Color.FromArgb(19, 24, 34);
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            Label logo = TextLabel("H", 25F, FontStyle.Bold, accent, 30, 23, 45, 50);
            Controls.Add(logo);
            Controls.Add(TextLabel("HARNESS AGENT", 17F, FontStyle.Bold, ink, 83, 25, 610, 30));
            Controls.Add(TextLabel("本地运行 · 独立数据 · 随时掌控", 9F, FontStyle.Regular, muted, 85, 58, 610, 25));

            Panel card = new Panel { Location = new Point(30, 106), Size = new Size(680, 160), BackColor = Color.FromArgb(29, 35, 48) };
            statusDot = new Panel { Location = new Point(23, 27), Size = new Size(9, 9), BackColor = muted };
            card.Controls.Add(statusDot);
            statusTitle = TextLabel("准备启动", 15F, FontStyle.Bold, ink, 45, 16, 605, 34);
            statusDetail = TextLabel("正在准备独立运行环境…", 9F, FontStyle.Regular, muted, 22, 56, 636, 37);
            address = TextLabel("服务仅在本机运行", 10F, FontStyle.Regular, muted, 22, 111, 636, 26);
            card.Controls.Add(statusTitle);
            card.Controls.Add(statusDetail);
            card.Controls.Add(address);
            Controls.Add(card);

            openButton = ActionButton("打开 Agent 工作区", 30, 284, 240, true);
            openButton.Enabled = false;
            openButton.Click += delegate { if (!String.IsNullOrEmpty(currentUrl)) LocalService.Open(currentUrl); };
            restartButton = ActionButton("重启服务", 284, 284, 132, false);
            restartButton.Click += delegate { StopService(false, true); };
            toggleButton = ActionButton("停止服务", 430, 284, 132, false);
            toggleButton.Click += delegate { if (state == ServiceState.Stopped || state == ServiceState.Failed) StartService(); else StopService(false, false); };
            logsButton = ActionButton("查看日志", 576, 284, 134, false);
            logsButton.Click += delegate { OpenLog(); };
            Controls.Add(openButton);
            Controls.Add(restartButton);
            Controls.Add(toggleButton);
            Controls.Add(logsButton);

            Label dataTitle = TextLabel("本地数据", 9F, FontStyle.Bold, ink, 30, 352, 110, 23);
            dataTitle.AutoEllipsis = false;
            dataTitle.AutoSize = true;
            Controls.Add(dataTitle);
            int dataPathLeft = Math.Max(148, dataTitle.Right + 12);
            Label dataPath = TextLabel(home, 9F, FontStyle.Regular, muted, dataPathLeft, 352, 582 - dataPathLeft, 23);
            ToolTip tooltip = new ToolTip();
            tooltip.SetToolTip(dataPath, home);
            Controls.Add(dataPath);
            LinkLabel dataLink = new LinkLabel { Text = "打开数据目录", Font = new Font("Microsoft YaHei UI", 9F), Location = new Point(593, 352), Size = new Size(116, 24),
                LinkColor = accent, ActiveLinkColor = ink, VisitedLinkColor = accent };
            dataLink.LinkClicked += delegate { LocalService.Open(home); };
            Controls.Add(dataLink);

            credentialsButton = ActionButton("查看首次登录凭据", 30, 391, 181, false);
            credentialsButton.Height = 34;
            credentialsButton.Font = new Font("Microsoft YaHei UI", 9F);
            credentialsButton.Click += delegate { OpenCredentials(); };
            Controls.Add(credentialsButton);
            Controls.Add(TextLabel("首次登录后，请在设置中修改管理员密码并配置模型。", 8.5F, FontStyle.Regular, muted, 226, 400, 484, 25));
            footer = TextLabel("关闭此控制台会安全停止本次启动的服务。", 8F, FontStyle.Regular, muted, 30, 446, 680, 20);
            Controls.Add(footer);
        }

        private void StartService()
        {
            if (stopPending || (child != null && !HasExited(child))) return;
            CleanChild();
            string script = Path.Combine(LocalService.PackageRoot, "app", "local_agent.py");
            if (!File.Exists(LocalService.PythonPath) || !File.Exists(script))
            {
                SetState(ServiceState.Failed, "运行文件不完整", "请完整解压程序包，再打开 HarnessAgent.exe。需包含 runtime 和 app 目录。");
                return;
            }
            try
            {
                string logDir = Path.Combine(home, "logs");
                Directory.CreateDirectory(logDir);
                logPath = Path.Combine(logDir, "launcher-" + DateTime.Now.ToString("yyyyMMdd-HHmmss-fff") + ".log");
                lock (logLock) logWriter = new StreamWriter(new FileStream(logPath, FileMode.CreateNew, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)) { AutoFlush = true };
                ProcessStartInfo info = new ProcessStartInfo(LocalService.PythonPath,
                    "-X utf8 -u " + LocalService.Quote(script) + " serve --managed --home " + LocalService.Quote(home) + " --port 17650 --status-file " + LocalService.Quote(Path.Combine(home, "runtime.json")));
                info.WorkingDirectory = Path.Combine(LocalService.PackageRoot, "app");
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                info.WindowStyle = ProcessWindowStyle.Hidden;
                info.RedirectStandardInput = true;
                info.RedirectStandardOutput = true;
                info.RedirectStandardError = true;
                info.StandardOutputEncoding = Encoding.UTF8;
                info.StandardErrorEncoding = Encoding.UTF8;
                info.EnvironmentVariables["PYTHONUTF8"] = "1";
                info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                info.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
                child = new Process { StartInfo = info };
                child.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) WriteLog(e.Data); };
                child.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) WriteLog(e.Data); };
                WriteLog("Launching packaged Harness Agent. Data directory: " + home);
                child.Start();
                childJob = OwnedProcessJob.TryCreate(child);
                if (childJob == null) WriteLog("Windows process job unavailable; managed stdin shutdown remains active.");
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();
                launchTime = DateTime.UtcNow;
                currentUrl = null;
                unhealthyChecks = 0;
                SetState(ServiceState.Starting, "正在启动", "正在准备本地数据和服务；首次启动可能需要稍候。");
            }
            catch (Exception ex)
            {
                WriteLog("Startup error: " + ex.Message);
                // A successfully created child always remains tracked and stoppable.
                if (child != null && !HasExited(child)) SetState(ServiceState.Starting, "服务已启动，控制台连接异常", ex.Message + "；可停止服务后重试。");
                else { CleanChild(); SetState(ServiceState.Failed, "启动失败", ex.Message + "；请查看日志。"); }
            }
        }

        private void PollService()
        {
            credentialsButton.Enabled = File.Exists(Path.Combine(home, "first-run.txt"));
            if (child == null || stopPending || (state != ServiceState.Starting && state != ServiceState.Running)) return;
            if (Interlocked.CompareExchange(ref checkPending, 1, 0) != 0) return;
            Process captured = child;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    bool exited = HasExited(captured);
                    int exitCode = exited ? GetExitCode(captured) : 0;
                    ReadyStatus ready = exited ? null : LocalService.ReadStatus(home);
                    bool healthy = ready != null && ready.Pid == captured.Id && LocalService.IsHealthy(ready.Url);
                    OnUi(delegate
                    {
                        if (child != captured || stopPending) return;
                        if (exited)
                        {
                            WriteLog("Runner exited with code " + exitCode + ".");
                            CleanChild();
                            SetState(ServiceState.Failed, "服务已退出", "退出代码 " + exitCode + "。请查看日志后重新启动。");
                        }
                        else if (healthy)
                        {
                            unhealthyChecks = 0;
                            currentUrl = ready.Url;
                            SetState(ServiceState.Running, "Agent 已就绪", "后台服务运行正常。打开工作区即可登录、配置模型并开始任务。");
                            if (autoOpen && !browserOpened) { browserOpened = true; LocalService.Open(currentUrl); }
                        }
                        else if (state == ServiceState.Running)
                        {
                            unhealthyChecks++;
                            if (unhealthyChecks >= 3)
                            {
                                openButton.Enabled = false;
                                statusTitle.Text = "正在等待服务响应";
                                statusDetail.Text = "服务进程仍在运行，健康检查暂未通过。可查看日志或重启服务。";
                                statusDot.BackColor = Color.FromArgb(224, 177, 98);
                            }
                        }
                        else
                        {
                            int seconds = Math.Max(0, (int)(DateTime.UtcNow - launchTime).TotalSeconds);
                            statusDetail.Text = seconds < 120 ? "正在准备本地数据和服务，已等待 " + seconds + " 秒。" : "启动耗时较长（" + seconds + " 秒）。可查看日志了解进度，或停止后重试。";
                        }
                    });
                }
                catch (Exception ex) { WriteLog("Readiness check: " + ex.Message); }
                finally { Interlocked.Exchange(ref checkPending, 0); }
            });
        }

        private void StopService(bool closeWhenStopped, bool restartWhenStopped)
        {
            if (stopPending) return;
            if (child == null || HasExited(child))
            {
                CleanChild();
                if (closeWhenStopped) ExitForm();
                else if (restartWhenStopped) StartService();
                else SetState(ServiceState.Stopped, "服务已停止", "本地数据已保留。点击启动服务可继续使用。");
                return;
            }
            Process captured = child;
            stopPending = true;
            SetState(ServiceState.Stopping, "正在安全停止", "正在结束本次运行并保存状态，请稍候…");
            ThreadPool.QueueUserWorkItem(delegate
            {
                bool stopped = false;
                try
                {
                    WriteLog("Graceful stop requested.");
                    captured.StandardInput.WriteLine("stop");
                    captured.StandardInput.Flush();
                    stopped = captured.WaitForExit(40000);
                }
                catch (Exception ex) { WriteLog("Stop request: " + ex.Message); stopped = HasExited(captured); }
                OnUi(delegate
                {
                    if (child != captured) return;
                    stopPending = false;
                    if (!stopped && !HasExited(captured))
                    {
                        DialogResult answer = MessageBox.Show(this, "服务尚未完成退出。强制结束会中断正在进行的任务。\n\n是否强制结束本控制台启动的服务？\n选择“否”将继续保留控制台和服务。", "等待服务停止", MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2);
                        if (answer != DialogResult.Yes)
                        {
                            SetState(ServiceState.Starting, "仍在等待退出", "已发出停止请求。请查看日志；再次点击停止可继续等待。");
                            return;
                        }
                        try
                        {
                            WriteLog("User explicitly confirmed forced stop of the owned process tree.");
                            if (childJob != null) { childJob.Dispose(); childJob = null; }
                            else if (!HasExited(captured)) captured.Kill();
                            if (!captured.WaitForExit(5000))
                            {
                                SetState(ServiceState.Starting, "退出尚未完成", "未能确认服务停止。控制台保留本次服务，供继续检查。");
                                return;
                            }
                        }
                        catch (Exception ex)
                        {
                            if (!HasExited(captured))
                            {
                                SetState(ServiceState.Starting, "停止失败", ex.Message + "；请查看日志。");
                                return;
                            }
                        }
                    }
                    CleanChild();
                    if (closeWhenStopped) ExitForm();
                    else if (restartWhenStopped) StartService();
                    else SetState(ServiceState.Stopped, "服务已停止", "本地数据已保留。点击启动服务可继续使用。");
                });
            });
        }

        private void SetState(ServiceState value, string title, string detail)
        {
            state = value;
            statusTitle.Text = title;
            statusDetail.Text = detail;
            statusDot.BackColor = value == ServiceState.Running ? Color.FromArgb(95, 207, 163) :
                value == ServiceState.Failed ? Color.FromArgb(245, 128, 134) : value == ServiceState.Stopped ? muted : Color.FromArgb(224, 177, 98);
            openButton.Enabled = value == ServiceState.Running;
            restartButton.Enabled = value == ServiceState.Running || value == ServiceState.Starting;
            toggleButton.Enabled = value != ServiceState.Stopping;
            toggleButton.Text = value == ServiceState.Stopped || value == ServiceState.Failed ? "启动服务" : "停止服务";
            address.Text = value == ServiceState.Running ? currentUrl : value == ServiceState.Stopped || value == ServiceState.Failed ? "服务仅在本机运行 · 数据保存在此电脑" : "仅监听本机回环地址 · 不开放局域网访问";
            address.ForeColor = value == ServiceState.Running ? accent : muted;
            footer.Text = value == ServiceState.Stopping ? "请保持控制台打开，等待服务完成退出。" : "关闭此控制台会安全停止本次启动的服务。";
        }

        private void OpenCredentials()
        {
            string path = Path.Combine(home, "first-run.txt");
            if (!File.Exists(path))
            {
                MessageBox.Show(this, "首次登录凭据尚未生成，或已被移除。服务首次完成初始化后可查看。", "Harness Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            OpenTextFile(path);
        }

        private void OpenLog()
        {
            if (!String.IsNullOrEmpty(logPath) && File.Exists(logPath)) OpenTextFile(logPath);
            else
            {
                string logDir = Path.Combine(home, "logs");
                Directory.CreateDirectory(logDir);
                LocalService.Open(logDir);
            }
        }

        private void OpenTextFile(string path)
        {
            try
            {
                Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "notepad.exe"), LocalService.Quote(path)) { UseShellExecute = false });
            }
            catch { LocalService.Open(path); }
        }

        private void HandleClosing(object sender, FormClosingEventArgs e)
        {
            if (exiting) return;
            e.Cancel = true;
            StopService(true, false);
        }

        private void ExitForm()
        {
            exiting = true;
            timer.Stop();
            CleanChild();
            Close();
        }

        private void CleanChild()
        {
            if (child != null)
            {
                if (!HasExited(child)) return;
                // A crashed runner can leave children holding redirected output pipes.
                // End only this job before disposing the runner; never wait on a pipe forever.
                if (childJob != null) { childJob.Dispose(); childJob = null; }
                try { child.Dispose(); } catch { }
                child = null;
            }
            if (childJob != null) { childJob.Dispose(); childJob = null; }
            lock (logLock)
            {
                if (logWriter != null) { logWriter.Dispose(); logWriter = null; }
            }
            currentUrl = null;
        }

        private static bool HasExited(Process process)
        {
            try { return process.HasExited; } catch { return true; }
        }

        private static int GetExitCode(Process process)
        {
            try { return process.ExitCode; } catch { return -1; }
        }

        private void WriteLog(string message)
        {
            lock (logLock)
            {
                try { if (logWriter != null) logWriter.WriteLine(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message); }
                catch { }
            }
        }

        private void OnUi(Action action)
        {
            try { if (!IsDisposed && IsHandleCreated) BeginInvoke(action); } catch (InvalidOperationException) { }
        }
    }

    // The job contains only this launcher's child and its descendants. Closing it can
    // never terminate an unrelated listener or a process discovered from runtime.json.
    internal sealed class OwnedProcessJob : IDisposable
    {
        private IntPtr handle;
        private OwnedProcessJob(IntPtr value) { handle = value; }

        public static OwnedProcessJob TryCreate(Process process)
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return null;
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x00002000;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(job, 9, buffer, (uint)length) || !AssignProcessToJobObject(job, process.Handle))
                {
                    CloseHandle(job);
                    return null;
                }
                return new OwnedProcessJob(job);
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public void Dispose()
        {
            IntPtr old = Interlocked.Exchange(ref handle, IntPtr.Zero);
            if (old != IntPtr.Zero) CloseHandle(old);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll")]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
        [DllImport("kernel32.dll")]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr value);
    }
}
