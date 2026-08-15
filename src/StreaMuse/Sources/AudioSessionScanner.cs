using System.Diagnostics;
using NAudio.CoreAudioApi;
using NAudio.CoreAudioApi.Interfaces;

namespace StreaMuse.Sources;

/// <summary>A process currently holding a render session on the default output device.</summary>
public sealed record AudioSession(
    int ProcessId,
    string ProcessName,
    string DisplayName,
    bool Active,
    float Peak);

/// <summary>WASAPI render sessions: which processes are actually producing audio right now.</summary>
public sealed class AudioSessionScanner
{
    public IReadOnlyList<AudioSession> Scan()
    {
        var results = new List<AudioSession>();

        try
        {
            using var enumerator = new MMDeviceEnumerator();

            foreach (var device in enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active))
            {
                using (device) AddSessions(device, results);
            }
        }
        catch (Exception)
        {
        }

        return results;
    }

    private static void AddSessions(MMDevice device, List<AudioSession> results)
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
                        ProcessNameOf(pid),
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

    public static string ProcessNameOf(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return process.ProcessName;
        }
        catch (Exception)
        {
            return "unknown";
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
