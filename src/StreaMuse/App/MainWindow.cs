using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Win32;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.App;

/// <summary>Frameless host; the page draws its own title bar and posts chrome commands back.</summary>
public sealed partial class MainWindow : Form
{
    private const int DefaultWidth = 1080;

    private const int DefaultHeight = 820;

    private readonly WebView2 _web = new();
    private readonly string _startUrl;
    private readonly StateHub _hub;

    /// <summary>Outer size the window opens at; below it the layout collapses.</summary>
    private Size _baseMinimum;

    /// <summary>DPI <see cref="_baseMinimum"/> was measured at, so it can follow the window.</summary>
    private int _baseDpi;

    /// <summary>Height the page reports for the open details pane, in CSS pixels.</summary>
    private int _detailsHeight;

    /// <summary>Device pixels the window was grown by to fit the pane, and so may give back.</summary>
    private int _grownForDetails;

    public MainWindow(string startUrl, StateHub hub, AppSettings settings)
    {
        _startUrl = startUrl;
        _hub = hub;

        Text = "StreaMuse";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(DefaultWidth, DefaultHeight);

        _web.Dock = DockStyle.Fill;
        Controls.Add(_web);

        ApplyTheme(IsDark(settings.Theme));

        Load += async (_, _) => await InitializeWebViewAsync();
    }

    /// <summary>
    /// The ground the page is painted on: what shows before the first paint and wherever WebView2
    /// lags a resize. The page posts every later change, so this only has to resolve the setting
    /// once - Auto against the same Windows app theme the page reads as prefers-color-scheme.
    /// </summary>
    private void ApplyTheme(bool dark)
    {
        var ground = dark ? Color.FromArgb(0x14, 0x15, 0x17) : Color.FromArgb(0xF2, 0xF2, 0xF3);

        BackColor = ground;
        _web.DefaultBackgroundColor = ground;
    }

    private static bool IsDark(AppTheme theme) => theme switch
    {
        AppTheme.Dark => true,
        AppTheme.Light => false,
        _ => Registry.GetValue(
            @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "AppsUseLightTheme",
            1) is 0
    };

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            parameters.Style |= WsThickFrame | WsMaximizeBox;
            return parameters;
        }
    }

    protected override void OnLoad(EventArgs e)
    {
        base.OnLoad(e);

        ClientSize = new Size(DefaultWidth, DefaultHeight);
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);

        if (!_baseMinimum.IsEmpty) return;

        _baseMinimum = Size;
        _baseDpi = DeviceDpi;
        ApplyMinimumSize();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        ApplyMinimumSize();
    }

    private void ApplyMinimumSize()
    {
        if (_baseMinimum.IsEmpty) return;

        var wanted = new Size(
            _baseMinimum.Width * DeviceDpi / _baseDpi,
            _baseMinimum.Height * DeviceDpi / _baseDpi + LogicalToDeviceUnits(_detailsHeight));

        var ceiling = MaximizedRect(Screen.FromControl(this)).Size;
        var floor = new Size(
            Math.Min(wanted.Width, ceiling.Width),
            Math.Min(wanted.Height, ceiling.Height));

        var deficit = WindowState == FormWindowState.Normal ? Math.Max(0, floor.Height - Height) : 0;

        // Setting MinimumSize also lifts a window that is standing below the new floor.
        MinimumSize = floor;

        if (deficit == 0) return;

        _grownForDetails += deficit;
        KeepOnScreen();
    }

    private void SetDetailsHeight(double cssHeight)
    {
        var wanted = double.IsFinite(cssHeight) ? (int)Math.Ceiling(Math.Max(0, cssHeight)) : 0;
        if (wanted == _detailsHeight) return;

        var released = LogicalToDeviceUnits(_detailsHeight) - LogicalToDeviceUnits(wanted);
        _detailsHeight = wanted;
        ApplyMinimumSize();

        var giveBack = Math.Min(released, _grownForDetails);
        if (giveBack <= 0 || WindowState != FormWindowState.Normal) return;

        _grownForDetails -= giveBack;
        Height = Math.Max(MinimumSize.Height, Height - giveBack);
    }

    /// <summary>
    /// A drag of the window's own edge settles what size the user wants, so nothing is owed back to
    /// the pane afterwards.
    /// </summary>
    protected override void OnResizeEnd(EventArgs e)
    {
        base.OnResizeEnd(e);
        _grownForDetails = 0;
    }

    protected override void WndProc(ref Message m)
    {
        base.WndProc(ref m);

        if (m.Msg == WmGetMinMaxInfo) LimitMaximizeToWorkArea(m.LParam);
    }

    /// <summary>
    /// Without a caption Windows maximizes to the whole monitor, which swallows the taskbar. Restate
    /// that against the work area instead, keeping the frame overhang Windows applies itself so the
    /// page still reaches the visible edges. Runs after the base handler, which owns the min track
    /// size, and only the two maximize fields are rewritten.
    /// </summary>
    private void LimitMaximizeToWorkArea(IntPtr lParam)
    {
        var screen = Screen.FromControl(this);
        var maximized = MaximizedRect(screen);

        var info = Marshal.PtrToStructure<MinMaxInfo>(lParam);
        info.MaxPosition = new Point(maximized.X - screen.Bounds.X, maximized.Y - screen.Bounds.Y);
        info.MaxSize = maximized.Size;

        Marshal.StructureToPtr(info, lParam, false);
    }

    /// <summary>Where a maximized window lands: the work area, grown by the frame's own overhang.</summary>
    private Rectangle MaximizedRect(Screen screen)
    {
        var overhang = SizeFromClientSize(Size.Empty);

        return new Rectangle(
            screen.WorkingArea.X - overhang.Width / 2,
            screen.WorkingArea.Y - overhang.Height / 2,
            screen.WorkingArea.Width + overhang.Width,
            screen.WorkingArea.Height + overhang.Height);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MinMaxInfo
    {
        public Point Reserved;
        public Size MaxSize;
        public Point MaxPosition;
        public Size MinTrackSize;
        public Size MaxTrackSize;
    }

    /// <summary>Growing for the pane must not push the window off the bottom of its screen.</summary>
    private void KeepOnScreen()
    {
        var work = Screen.FromControl(this).WorkingArea;

        var top = Bottom > work.Bottom ? work.Bottom - Height : Top;
        if (top < work.Top) top = work.Top;
        if (top != Top) Top = top;
    }

    private async Task InitializeWebViewAsync()
    {
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(Paths.DataDir, "webview"));

            await _web.EnsureCoreWebView2Async(env);

            var core = _web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.WebMessageReceived += OnWebMessage;

            core.Navigate(_startUrl);
        }
        catch (Exception ex)
        {
            _hub.Log(LineLevel.Error, $"WebView2 failed to start: {ex.Message}");
            MessageBox.Show(
                $"The WebView2 runtime could not be started.\n\n{ex.Message}\n\n" +
                $"The control panel is still available at {_startUrl}",
                "StreaMuse", MessageBoxButtons.OK, MessageBoxIcon.Warning);
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

        if (command?.Command is null) return;

        switch (command.Command)
        {
            case "minimize":
                WindowState = FormWindowState.Minimized;
                break;

            case "maximize":
                WindowState = WindowState == FormWindowState.Maximized
                    ? FormWindowState.Normal
                    : FormWindowState.Maximized;
                break;

            case "close":
                BeginInvoke(Close);
                break;

            case "drag":
                BeginDrag();
                break;

            case "detailsHeight":
                SetDetailsHeight(command.Height ?? 0);
                break;

            case "theme":
                ApplyTheme(command.Dark ?? false);
                break;
        }
    }

    /// <summary>Hands the drag to the window manager so snapping stays native.</summary>
    private void BeginDrag()
    {
        ReleaseCapture();
        SendMessage(Handle, WmNcLButtonDown, HtCaption, IntPtr.Zero);
    }

    private const int WmNcLButtonDown = 0x00A1;
    private const int HtCaption = 0x0002;
    private const int WsThickFrame = 0x00040000;
    private const int WsMaximizeBox = 0x00010000;
    private const int WmGetMinMaxInfo = 0x0024;

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private sealed record HostCommand(string? Command, double? Height, bool? Dark);

    protected override void Dispose(bool disposing)
    {
        if (disposing) _web.Dispose();
        base.Dispose(disposing);
    }
}
