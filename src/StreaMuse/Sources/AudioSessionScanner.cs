using NAudio.CoreAudioApi;
using NAudio.CoreAudioApi.Interfaces;

namespace StreaMuse.Sources;

/// <summary>A process holding a render session on some active output device.</summary>
public sealed record AudioSession(
    int ProcessId,
    string ProcessName,
    string DisplayName,
    bool Active,
    float Peak);

public static class AudioSessionScanner
{
    public static IReadOnlyList<AudioSession> Scan(ProcessTree tree)
    {
        var results = new List<AudioSession>();

        try
        {
            using var enumerator = new MMDeviceEnumerator();

            foreach (var device in enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active))
            {
                using (device) AddSessions(device, tree, results);
            }
        }
        catch (Exception)
        {
        }

        return results;
    }

    private static void AddSessions(MMDevice device, ProcessTree tree, List<AudioSession> results)
    {
        try
        {
            var manager = device.AudioSessionManager;
            manager.RefreshSessions();

            var sessions = manager.Sessions;
            for (var i = 0; i < sessions.Count; i++)
            {
                var session = sessions[i];
                try
                {
                    if (session.IsSystemSoundsSession) continue;

                    var pid = (int)session.GetProcessID;
                    if (pid <= 0) continue;

                    results.Add(new AudioSession(
                        pid,
                        tree.NameOf(pid) ?? "unknown",
                        SafeDisplayName(session),
                        session.State == AudioSessionState.AudioSessionStateActive,
                        SafePeak(session)));
                }
                catch (Exception)
                {
                }
            }
        }
        catch (Exception)
        {
        }
    }

    private static string SafeDisplayName(AudioSessionControl session)
    {
        try
        {
            return session.DisplayName ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }

    private static float SafePeak(AudioSessionControl session)
    {
        try
        {
            return session.AudioMeterInformation.MasterPeakValue;
        }
        catch (Exception)
        {
            return 0f;
        }
    }
}
