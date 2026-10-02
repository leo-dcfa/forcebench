// Relays the Dev Hub login's OAuth redirect into sf: the Makefile publishes 127.0.0.1:1717 on the
// host to port 1718 here, and sf's callback server listens on this container's localhost:1717,
// which may be ::1 or 127.0.0.1 (tried in that order: Node's own pick of the two can miss).
const net = require("net");

function upstream(client, hosts) {
  const sf = net.connect(1717, hosts[0]);
  let connected = false;
  sf.once("connect", () => {
    connected = true;
    client.pipe(sf);
    sf.pipe(client);
    client.resume();
  });
  sf.on("error", () => {
    if (!connected && hosts.length > 1) upstream(client, hosts.slice(1));
    else client.destroy();
  });
  client.on("error", () => sf.destroy());
}

net.createServer({ pauseOnConnect: true }, (client) => upstream(client, ["::1", "127.0.0.1"])).listen(1718, "0.0.0.0");
