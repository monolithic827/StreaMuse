using System.Collections.Concurrent;

namespace StreaMuse.Sources;

/// <summary>
/// The one thread every media transport control call runs on. The session proxies are bound to the
/// thread that created their manager, so a continuation resuming on another pool thread makes every
/// later call fail with RPC_E_WRONG_THREAD. See CLAUDE.md.
/// </summary>
public sealed class MediaThread
{
    private readonly BlockingCollection<Action> _work = new();

    public MediaThread()
    {
        var thread = new Thread(Loop) { IsBackground = true, Name = "smtc" };
        thread.Start();
    }

    public Task<T> RunAsync<T>(Func<Task<T>> work)
    {
        var completion = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);

        _work.Add(async void () =>
        {
            try
            {
                completion.SetResult(await work());
            }
            catch (Exception ex)
            {
                completion.SetException(ex);
            }
        });

        return completion.Task;
    }

    // Each await inside the queued work posts its continuation back onto this same queue, so the
    // whole call stays on this thread rather than resuming wherever the pool puts it.
    private void Loop()
    {
        SynchronizationContext.SetSynchronizationContext(new QueueContext(_work));

        foreach (var item in _work.GetConsumingEnumerable()) item();
    }

    private sealed class QueueContext(BlockingCollection<Action> work) : SynchronizationContext
    {
        public override void Post(SendOrPostCallback callback, object? state) =>
            work.Add(() => callback(state));
    }
}
