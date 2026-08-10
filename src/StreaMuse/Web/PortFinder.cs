using System.Net;
using System.Net.Sockets;

namespace StreaMuse.Web;

public static class PortFinder
{
    /// <summary>The preferred port if free on loopback, else an ephemeral one.</summary>
    public static int Pick(int preferred)
    {
        if (IsFree(preferred)) return preferred;

        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static bool IsFree(int port)
    {
        try
        {
            using var listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
            listener.Stop();
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }
}
