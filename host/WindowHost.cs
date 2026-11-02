// scene-cut 原生窗口宿主：WinForms + WebView2（进程内嵌，不启动 msedge.exe）
// 编译：tools/build_window.py（csc /target:winexe）
using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace SceneCutHost
{
    internal static class Program
    {
        private static readonly string LogPath = Path.Combine(Path.GetTempPath(), "scene-cut-window.log");

        private static void Log(string msg)
        {
            try { File.AppendAllText(LogPath, DateTime.Now.ToString("HH:mm:ss") + " " + msg + Environment.NewLine); }
            catch { }
        }

        [STAThread]
        private static void Main(string[] args)
        {
            Log("start args=" + string.Join(" ", args));
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string url = args.Length > 0 ? args[0] : "http://127.0.0.1:8768/";
            string dataDir = args.Length > 1 ? args[1] : Path.Combine(Path.GetTempPath(), "scene-cut-webview2");

            var form = new Form();
            form.Text = "scene-cut";
            form.MinimumSize = new Size(1200, 780);
            form.Size = new Size(1520, 960);
            form.StartPosition = FormStartPosition.CenterScreen;
            form.BackColor = Color.FromArgb(14, 17, 22);

            var web = new WebView2();
            web.Dock = DockStyle.Fill;
            form.Controls.Add(web);

            form.Shown += async (sender, e) =>
            {
                try
                {
                    Log("creating webview2 env, dataDir=" + dataDir);
                    Directory.CreateDirectory(dataDir);
                    CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(null, dataDir, null);
                    await web.EnsureCoreWebView2Async(env);
                    Log("webview2 ready, navigating");
                    web.CoreWebView2.Settings.AreDevToolsEnabled = false;
                    web.CoreWebView2.DocumentTitleChanged += (s2, e2) =>
                    {
                        try { form.Text = web.CoreWebView2.DocumentTitle; } catch { }
                    };
                    web.CoreWebView2.NavigationCompleted += (s2, e2) =>
                    {
                        Log("navigation completed success=" + e2.IsSuccess + " url=" + (web.CoreWebView2.Source ?? ""));
                        if (!e2.IsSuccess)
                        {
                            MessageBox.Show("页面加载失败：" + e2.WebErrorStatus, "scene-cut",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        }
                    };
                    web.CoreWebView2.Navigate(url);
                }
                catch (Exception ex)
                {
                    Log("init failed: " + ex);
                    MessageBox.Show("WebView2 初始化失败：\n" + ex.Message + "\n\n日志：" + LogPath,
                        "scene-cut", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    form.Close();
                }
            };

            form.FormClosed += (sender, e) => Application.Exit();
            Application.Run(form);
        }
    }
}
