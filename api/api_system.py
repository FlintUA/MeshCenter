import subprocess

from flask import jsonify, request
import subprocess


def register_system_routes(app):

    def get_saved_wifi_names():
        try:
            out = subprocess.check_output(
                ["sudo", "-n", "/usr/bin/nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10
            )

            saved = set()

            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == "802-11-wireless":
                    saved.add(parts[0])

            return saved

        except Exception:
            return set()    

    @app.route("/api/system/wifi/scan")
    def api_system_wifi_scan():
        result = {
            "ok": True,
            "networks": []
        }

        try:
            out = subprocess.check_output(
                ["sudo", "-n", "/usr/sbin/iw", "dev", "wlan0", "scan"],
                text=True,
                stderr=subprocess.DEVNULL
            )

            networks = []
            current = None

            for raw_line in out.splitlines():
                line = raw_line.strip()

                if line.startswith("BSS "):
                    if current and current.get("ssid"):
                        networks.append(current)

                    bssid = line.split()[1].split("(")[0]

                    current = {
                        "ssid": None,
                        "bssid": bssid,
                        "signal_dbm": None,
                        "signal": None,
                        "frequency": None,
                        "channel": None,
                        "security": "Open",
                        "connected": False
                    }

                elif current is not None and line.startswith("SSID:"):
                    current["ssid"] = line.replace("SSID:", "").strip()

                elif current is not None and line.startswith("signal:"):
                    try:
                        dbm = float(
                            line.replace("signal:", "")
                                .replace("dBm", "")
                                .strip()
                        )
                        current["signal_dbm"] = round(dbm, 1)
                        current["signal"] = max(0, min(100, int(2 * (dbm + 100))))
                    except Exception:
                        pass

                elif current is not None and line.startswith("freq:"):
                    try:
                        freq = int(line.replace("freq:", "").strip())
                        current["frequency"] = freq

                        if 2412 <= freq <= 2484:
                            current["band"] = "2.4 GHz"
                            current["channel"] = int((freq - 2407) / 5)
                        elif 5000 <= freq <= 5900:
                            current["band"] = "5 GHz"
                            current["channel"] = int((freq - 5000) / 5)
                        else:
                            current["band"] = "--"
                    except Exception:
                        pass

                elif current is not None:
                    if "RSN:" in line:
                        current["security"] = "WPA2/WPA3"
                    elif "WPA:" in line and current["security"] == "Open":
                        current["security"] = "WPA"

            if current and current.get("ssid"):
                networks.append(current)

            try:
                link = subprocess.check_output(
                    ["/usr/sbin/iw", "dev", "wlan0", "link"],
                    text=True
                )

                connected_ssid = None
                connected_bssid = None

                for line in link.splitlines():
                    line = line.strip()

                    if line.startswith("Connected to "):
                        connected_bssid = line.split()[2].lower()
                    elif line.startswith("SSID:"):
                        connected_ssid = line.replace("SSID:", "").strip()

                for net in networks:
                    if (
                        connected_bssid
                        and net.get("bssid", "").lower() == connected_bssid
                    ) or (
                        connected_ssid
                        and net.get("ssid") == connected_ssid
                    ):
                        net["connected"] = True

            except Exception:
                pass

            by_ssid = {}

            for net in networks:
                ssid = net.get("ssid")
                if not ssid:
                    continue

                old = by_ssid.get(ssid)
                if old is None or (net.get("signal") or 0) > (old.get("signal") or 0):
                    by_ssid[ssid] = net

            saved_wifi = get_saved_wifi_names()

            result["networks"] = list(by_ssid.values())

            for net in result["networks"]:
                net["saved"] = net.get("ssid") in saved_wifi

            result["networks"].sort(
                key=lambda n: (
                    not n.get("connected", False),
                    -(n.get("signal") or 0)
                )
            )

        except Exception as e:
            return jsonify({
                "ok": False,
                "error": str(e),
                "networks": []
            }), 500

        return jsonify(result)


    @app.route("/api/system/wifi/connect", methods=["POST"])
    def api_system_wifi_connect():
        data = request.get_json(force=True)

        ssid = (data.get("ssid") or "").strip()
        password = data.get("password") or ""

        if not ssid:
            return jsonify({"ok": False, "error": "SSID is required"}), 400

        def run_nmcli(args, timeout=30):
            return subprocess.check_output(
                ["sudo", "-n", "/usr/bin/nmcli"] + args,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=timeout
            )

        try:
            # First try normal connect
            cmd = ["dev", "wifi", "connect", ssid]

            if password:
                cmd += ["password", password]

            out = run_nmcli(cmd)

            return jsonify({
                "ok": True,
                "message": out.strip()
            })

        except subprocess.CalledProcessError as e:
            err = e.output.strip() if e.output else str(e)

            # Fix broken/stale NetworkManager profile and retry once
            if "key-mgmt" in err or "property is missing" in err or "secrets were required" in err:
                try:
                    run_nmcli(["connection", "delete", ssid], timeout=10)
                except Exception:
                    pass

                try:
                    cmd = ["dev", "wifi", "connect", ssid]

                    if password:
                        cmd += ["password", password]

                    out = run_nmcli(cmd)

                    return jsonify({
                        "ok": True,
                        "message": out.strip(),
                        "recreated_profile": True
                    })

                except subprocess.CalledProcessError as e2:
                    return jsonify({
                        "ok": False,
                        "error": e2.output.strip() if e2.output else str(e2)
                    }), 500

            return jsonify({
                "ok": False,
                "error": err
            }), 500

        except Exception as e:
            return jsonify({
                "ok": False,
                "error": str(e)
            }), 500

    @app.route("/api/system/wifi/forget", methods=["POST"])
    def api_system_wifi_forget():
        data = request.get_json(force=True)

        ssid = (data.get("ssid") or "").strip()

        if not ssid:
            return jsonify({"ok": False, "error": "SSID is required"}), 400

        try:
            out = subprocess.check_output(
                ["sudo", "-n", "/usr/bin/nmcli", "connection", "delete", ssid],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=15
            )

            return jsonify({
                "ok": True,
                "message": out.strip()
            })

        except subprocess.CalledProcessError as e:
            return jsonify({
                "ok": False,
                "error": e.output.strip() if e.output else str(e)
            }), 500

        except Exception as e:
            return jsonify({
                "ok": False,
                "error": str(e)
            }), 500

    @app.route("/api/system/network")
    def api_system_network():

        result = {
            "ssid": None,
            "signal_percent": None,
            "rssi_dbm": None,
            "ip": None,
            "gateway": None,
            "internet": False
        }

        #
        # SSID
        #

        try:
            ssid = subprocess.check_output(
                ["iwgetid", "-r"],
                text=True
            ).strip()

            result["ssid"] = ssid

        except Exception:
            pass

        #
        # Signal / Wi-Fi link
        #

        try:
            out = subprocess.check_output(
                ["/usr/sbin/iw", "dev", "wlan0", "link"],
                text=True
            )

            for line in out.splitlines():
                line = line.strip()

                if line.startswith("SSID:"):
                    result["ssid"] = line.replace("SSID:", "").strip()

                elif line.startswith("signal:"):
                    dbm = int(line.replace("signal:", "").replace("dBm", "").strip())
                    result["rssi_dbm"] = dbm
                    result["signal_percent"] = max(0, min(100, 2 * (dbm + 100)))

                elif line.startswith("rx bitrate:"):
                    result["rx_bitrate"] = line.replace("rx bitrate:", "").strip()

                elif line.startswith("tx bitrate:"):
                    result["tx_bitrate"] = line.replace("tx bitrate:", "").strip()

        except Exception:
            pass

        #
        # IP
        #

        try:

            ip = subprocess.check_output(
                ["hostname", "-I"],
                text=True
            ).strip().split()

            if ip:
                result["ip"] = ip[0]

        except Exception:
            pass

        #
        # Gateway
        #

        try:

            route = subprocess.check_output(
                ["ip", "route"],
                text=True
            )

            for line in route.splitlines():

                if line.startswith("default"):

                    result["gateway"] = line.split()[2]

        except Exception:
            pass

        #
        # Internet
        #

        try:

            subprocess.check_call(
                ["ping", "-c", "1", "-W", "1", "8.8.8.8"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            result["internet"] = True

        except Exception:
            pass

        return jsonify(result)
    