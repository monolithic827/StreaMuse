using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using StreaMuse.State;

namespace StreaMuse.App;

/// <summary>The decks, in their own top-level window so they can sit beside the panel rather than on
/// top of it. Frameless like <see cref="MainWindow"/> and for the same reason - the page draws its own
/// title bar - but with none of the details-pane sizing, since nothing here grows the window.</summary>
public sealed class DjWindow : Form
{
    private const int DefaultWidth = 460;
    private const int DefaultHeight = 620;

    private readonly WebView2 _web = new();
    private readonly CoreWebView2Environment _environment;
    private readonly string _startUrl;
    private readonly StateHub _hub;

    public DjWindow(CoreWebView2Environment environment, string startUrl, StateHub hub, Color ground)
    {
        _environment = environment;
        _startUrl = startUrl;
        _hub = hub;

        Text = "StreaMuse DJ";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        ClientSize = new Size(DefaultWidth, DefaultHeight);
        MinimumSize = new Size(360, 420);

        BackColor = ground;
        _web.DefaultBackgroundColor = ground;
        _web.Dock = DockStyle.Fill;
        Controls.Add(_web);

        Load += async (_, _) => await InitializeWebViewAsync();
    }

    /// <summary>Opens beside the panel rather than centred over it, since the point of a separate
    /// window is seeing both at once. Falls back inside the work area when there is no room.</summary>
    public void PlaceBeside(Form owner)
    {
        var work = Screen.FromControl(owner).WorkingArea;
        var left = owner.Right + 12;

        if (left + Width > work.Right) left = Math.Max(work.Left, owner.Left - Width - 12);

        Location = new Point(
            Math.Clamp(left, work.Left, Math.Max(work.Left, work.Right - Width)),
            Math.Clamp(owner.Top, work.Top, Math.Max(work.Top, work.Bottom - Height)));
    }

    public void ApplyTheme(Color ground)
    {
        BackColor = ground;
        _web.DefaultBackgroundColor = ground;
    }

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            parameters.Style |= WsThickFrame;
            return parameters;
        }
    }

    /// <summary>Closing hides rather than disposes, so reopening does not pay for WebView2 startup
    /// again and the socket the page holds stays live. The real teardown is Dispose, at app exit.</summary>
    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (e.CloseReason == CloseReason.UserClosing || e.CloseReason == CloseReason.None)
        {
            e.Cancel = true;
            Hide();
            return;
        }

        base.OnFormClosing(e);
    }

    private async Task InitializeWebViewAsync()
    {
        try
        {
            await _web.EnsureCoreWebView2Async(_environment);

            var core = _web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.WebMessageReceived += OnWebMessage;

            core.Navigate(_startUrl);
        }
        catch (Exception ex)
        {
            _hub.Log(LineLevel.Error, $"DJ window failed to start: {ex.Message}");
        }
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string json;
        try
        {
            json = e.WebMessageAsJson;
        }
        catch (Exception)
        {
            return;
        }

        HostCommand? command;
        try
        {
            command = JsonSerializer.Deserialize<HostCommand>(json, JsonOptions);
        }
        catch (JsonException)
        {
            return;
        }

        switch (command?.Command)
        {
            case "minimize":
                WindowState = FormWindowState.Minimized;
                break;

            case "close":
                // The command arrives on a WebView2 callback; closing here would dispose the WebView2
                // that is mid-dispatch. Same reason as MainWindow.
                BeginInvoke(Hide);
                break;

            case "drag":
                ReleaseCapture();
                SendMessage(Handle, WmNcLButtonDown, HtCaption, IntPtr.Zero);
                break;
        }
    }

    private const int WmNcLButtonDown = 0x00A1;
    private const int HtCaption = 0x0002;
    private const int WsThickFrame = 0x00040000;

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private sealed record HostCommand(string? Command);

    protected override void Dispose(bool disposing)
    {
        if (disposing) _web.Dispose();
        base.Dispose(disposing);
    }
}
