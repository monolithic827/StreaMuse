using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using StreaMuse.State;

namespace StreaMuse.App;

/// <summary>A small fixed-size dialog styled like a Windows 95/98 About box. There is deliberately no
/// menu item, button or setting that opens this - see how it is reached in app.js. Frameless like
/// <see cref="MainWindow"/>/<see cref="DjWindow"/> so the page can draw its own (fake, period) chrome
/// instead of the real one, and always painted in the classic grey - it does not follow the app's
/// light/dark theme, since the point is looking like it is from an OS that predates that setting.</summary>
public sealed class RetroWindow : Form
{
    private const int Width95 = 420;
    private const int Height95 = 320;

    private static readonly Color Face = Color.FromArgb(0xC0, 0xC0, 0xC0);

    private readonly WebView2 _web = new();
    private readonly CoreWebView2Environment _environment;
    private readonly string _startUrl;
    private readonly StateHub _hub;

    public RetroWindow(CoreWebView2Environment environment, string startUrl, StateHub hub)
    {
        _environment = environment;
        _startUrl = startUrl;
        _hub = hub;

        Text = "About StreaMuse";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        ClientSize = new Size(Width95, Height95);

        BackColor = Face;
        _web.DefaultBackgroundColor = Face;
        _web.Dock = DockStyle.Fill;
        Controls.Add(_web);

        Load += async (_, _) => await InitializeWebViewAsync();
    }

    /// <summary>Centred on the owner, like a dialog - this is not a workspace window someone arranges
    /// beside the panel, it is a thing that pops up and gets dismissed.</summary>
    public void PlaceCenter(Form owner)
    {
        var work = Screen.FromControl(owner).WorkingArea;

        Location = new Point(
            Math.Clamp(owner.Left + (owner.Width - Width) / 2, work.Left, Math.Max(work.Left, work.Right - Width)),
            Math.Clamp(owner.Top + (owner.Height - Height) / 2, work.Top, Math.Max(work.Top, work.Bottom - Height)));
    }

    /// <summary>Closing hides rather than disposes, same reasoning as <see cref="DjWindow"/> - skip
    /// paying for WebView2 startup again on the (surely inevitable) second visit.</summary>
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
            _hub.Log(LineLevel.Warn, $"retro window failed to start: {ex.Message}");
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
            case "close":
                // Arrives on a WebView2 callback; closing here would dispose the WebView2 mid-dispatch.
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
