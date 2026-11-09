#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Threading;
using Notepad.ViewModels;
using OFT.Docking.ViewModels.Panels;
using OFT.Platform.Core.Settings.WebPages;
using OFT.Platform.Core.ViewModels.WebViewControls;
using OFT.PlatformX.ViewModels;
using OFT.PlatformX.ViewModels.Workspaces;
using OFT.PlatformX.Views.WebViewControls;
using OFT.Rendering.Avn.Native;

// Version-coupled to the installed public ATAS X 8.0.15 API. No reflection,
// private fields, host patches, floating window, or browser fallback.
internal static class ReviewDockHost
{
    internal const string PanelId = "atas-ai-bridge.review.v1";
    private const string Title = "AI 复盘";
    private static readonly SemaphoreSlim Gate = new SemaphoreSlim(1, 1);
    private static int _users;
    private static Binding _binding; // one subscription set across all indicator instances

    internal static bool IsOwnedPanel(string id, bool webPages, string url) =>
        id == PanelId && webPages && ReviewLaunchResult.IsRootUrl(url);

    internal static async Task<string> OpenInstalledAsync(int bridgePort, CancellationToken cancellation)
    {
        await Gate.WaitAsync(cancellation).ConfigureAwait(false);
        try
        {
            await Dispatcher.UIThread.InvokeAsync(CheckAllowed);
            var result = await ReviewLaunch.InstalledAsync(bridgePort, cancellation).ConfigureAwait(false);
            cancellation.ThrowIfCancellationRequested();
            var panel = await Dispatcher.UIThread.InvokeAsync(() => FindOrCreate(result.Url));
            Binding binding = null;
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (binding == null && DateTime.UtcNow < deadline)
            {
                cancellation.ThrowIfCancellationRequested();
                binding = await Dispatcher.UIThread.InvokeAsync(() => BindIfReady(panel));
                if (binding == null) await Task.Delay(50, cancellation).ConfigureAwait(false);
            }
            if (binding == null)
                throw new InvalidOperationException("ATAS review WebView did not initialize; reopen from indicator settings.");
            try
            {
                await Dispatcher.UIThread.InvokeAsync(() => binding.Begin(result));
                await binding.Completion.WaitAsync(TimeSpan.FromSeconds(10), cancellation).ConfigureAwait(false);
                return "AI review tab | owned panel | " + result.Url + " | native session authenticated";
            }
            finally
            {
                // Remove token-bearing pending state on timeout/cancellation as well.
                await Dispatcher.UIThread.InvokeAsync(binding.ClearPending);
            }
        }
        catch (TimeoutException)
        {
            throw new InvalidOperationException("ATAS review navigation timed out; reopen from indicator settings.");
        }
        finally { Gate.Release(); }
    }

    private static PanelDockFactory CheckAllowed()
    {
        var factory = PanelDockFactory.Instance;
        if (factory == null || factory.WorkspaceManager == null)
            throw new InvalidOperationException("ATAS workspace API is unavailable.");
        // This is the host's combined license/replay/brand gate, including reuse.
        if (!factory.CanOpenWidget(WorkspaceLayoutType.WebPages))
            throw new InvalidOperationException("ATAS does not allow the WebPages panel (license, replay or brand restriction).");
        return factory;
    }

    private static PanelViewModel FindOrCreate(string url)
    {
        if (!ReviewLaunchResult.IsRootUrl(url)) throw new InvalidOperationException("Invalid review root URL.");
        var factory = CheckAllowed();
        var marked = factory.WorkspaceManager.GetAllPanels().Where(p => p.Id == PanelId).ToArray();
        if (marked.Length > 1)
            throw new InvalidOperationException("Multiple marked AI review panels; close duplicates before reopening.");
        if (marked.SingleOrDefault() is PanelViewModel panel)
        {
            if (!IsOwnedPanel(panel.Id, panel.WorkspaceLayoutType == WorkspaceLayoutType.WebPages,
                              (panel.GetParameter() as WebPagesSettings)?.CurrentUrl))
                throw new InvalidOperationException("Marked review panel has an unexpected type or URL; it was not changed.");
            panel.CanClonePanel = false;
            var tab = panel.MainViewModel?.Tabs.FirstOrDefault(t => t.GetPanels().Contains(panel));
            if (tab != null) panel.MainViewModel.SelectedTab = tab;
            panel.Activate();
            return panel;
        }
        if (marked.Length != 0)
            throw new InvalidOperationException("Marked review panel is incompatible with this host.");
        var manager = factory.WorkspaceManager;
        var main = manager.ActiveWindow?.DataContext as MainViewModel ??
                   manager.ActiveWindow?.Owner?.DataContext as MainViewModel;
        if (main == null)
            throw new InvalidOperationException("An active ATAS workspace window is required to open the review tab.");
        panel = factory.CreateWidget(WorkspaceLayoutType.WebPages, new WebPagesSettings { CurrentUrl = url }, PanelId);
        if (panel == null) throw new InvalidOperationException("ATAS did not create the review WebPages widget.");
        panel.CustomTitle = Title;
        panel.CanClonePanel = false;
        var layout = factory.CreateLayout(panel);
        main.AddTab(layout, Title, true);
        panel.Activate();
        return panel;
    }

    private static Binding BindIfReady(PanelViewModel panel)
    {
        // Navigator is the public owning WebPages view. Its Content is the host's
        // WebViewControl in this SDK; never search unrelated windows or web pages.
        if (!(panel.Content is WebPagesViewModel model) ||
            !(model.Navigator is WebPagesView view) || !(view.Content is WebViewControl web) || !web.IsLoaded)
            return null;
        if (_binding != null && ReferenceEquals(_binding.Panel, panel) && ReferenceEquals(_binding.Web, web))
            return _binding;
        _binding?.Dispose();
        _binding = new Binding(panel, model, web);
        return _binding;
    }

    internal static void Retain() => Interlocked.Increment(ref _users);

    internal static void Release()
    {
        if (Interlocked.Decrement(ref _users) != 0) return;
        Dispatcher.UIThread.Post(() =>
        {
            if (Volatile.Read(ref _users) != 0) return;
            _binding?.Dispose(); _binding = null;
        });
    }

    private sealed class Binding : IDisposable
    {
        internal PanelViewModel Panel { get; }
        internal WebViewControl Web { get; }
        private readonly WebPagesViewModel _model;
        private ReviewBootstrapDelivery _delivery;
        private TaskCompletionSource<bool> _completion;
        private bool _disposed;
        internal Task Completion => _completion.Task;

        internal Binding(PanelViewModel panel, WebPagesViewModel model, WebViewControl web)
        {
            Panel = panel; _model = model; Web = web;
            Web.NavigationCompleted += Navigated;
            Web.ErrorOccurred += Failed;
            Web.WebMessageReceived += Received;
            Panel.Closed += Closed;
        }

        internal void Begin(ReviewLaunchResult result)
        {
            if (_disposed) throw new InvalidOperationException("Review panel was closed.");
            ClearPending();
            _completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _delivery = new ReviewBootstrapDelivery(result);
            _model.Source = new Uri(result.Url);
            // A full navigation on explicit reopen handles expired sessions and
            // guarantees the head bootstrap listener is installed before delivery.
            Web.Navigate(result.Url);
        }

        private void Navigated(object sender, WebViewNavigationCompletedEventArgs args)
        {
            if (!Dispatcher.UIThread.CheckAccess())
            { Dispatcher.UIThread.Post(() => Navigated(sender, args)); return; }
            if (_delivery == null || _disposed) return;
            // WebPages starts its own initial navigation. Our explicit reopen may
            // cancel it; a failed/canceled completion cannot authenticate anything.
            // Wait for the succeeding navigation, ErrorOccurred, or bounded timeout.
            if (!args.IsSuccess) return;
            try
            {
                var message = _delivery.TakeMessage(Web.CurrentUrl?.OriginalString, args.IsSuccess, Web.IsWebMessagingSupported);
                if (message == null) return;
                Web.PostWebMessageAsJson(message);
            }
            catch (Exception)
            {
                ClearPending();
                _completion.TrySetException(new InvalidOperationException("Review page URL, navigation or native messaging failed verification."));
            }
        }

        private void Received(WebMessageReceivedArgs args)
        {
            if (!Dispatcher.UIThread.CheckAccess()) { Dispatcher.UIThread.Post(() => Received(args)); return; }
            if (_disposed || _delivery == null ||
                !_delivery.TryAcceptAcknowledgement(args.MessageAsJson, args.Source, Web.CurrentUrl?.OriginalString, out var ok))
                return;
            ClearPending();
            if (ok) _completion.TrySetResult(true);
            else _completion.TrySetException(new InvalidOperationException("Review session bootstrap was rejected; reopen from indicator settings."));
        }

        private void Failed(string ignored)
        {
            if (!Dispatcher.UIThread.CheckAccess()) { Dispatcher.UIThread.Post(() => Failed(null)); return; }
            ClearPending();
            _completion?.TrySetException(new InvalidOperationException("ATAS review WebView failed; capture is still running."));
        }

        private void Closed(IPanelViewModel ignored)
        {
            if (!Dispatcher.UIThread.CheckAccess()) { Dispatcher.UIThread.Post(() => Closed(null)); return; }
            Dispose();
        }
        internal void ClearPending() { _delivery?.Clear(); _delivery = null; }
        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            ClearPending();
            Web.NavigationCompleted -= Navigated;
            Web.ErrorOccurred -= Failed;
            Web.WebMessageReceived -= Received;
            Panel.Closed -= Closed;
            if (ReferenceEquals(_binding, this)) _binding = null;
            _completion?.TrySetException(new InvalidOperationException("AI review panel was closed."));
            // The host owns the WebView and panel; never dispose/close either here.
        }
    }
}

// The public host custom-editor locator supports EditorAttribute -> Control.
// A settings value or deserialization can never execute this action: only Click.
public sealed class ReviewOpenAction
{
    private readonly Action _open;
    internal ReviewOpenAction(Action open) { _open = open; }
    public void Execute() { _open(); }
    public override string ToString() => "Open AI review tab";
}

public sealed class ReviewOpenEditor : Button
{
    public ReviewOpenEditor()
    {
        Content = "Open AI review tab";
        Click += (_, _) => (DataContext as ReviewOpenAction)?.Execute();
    }
}
#endif
