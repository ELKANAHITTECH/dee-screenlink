<<<<<<< HEAD
# ScreenLink – Windows 11 → Android second screen

```
screenlink/
├── server/   Python app for Windows (screen capture + input injection)
└── client/   Flutter app for Android (viewer + touch/keyboard)
```

## 1. Windows server

```powershell
cd server
pip install -r requirements.txt
python screenlink_server.py
```

The console prints a **PIN** and your PC's **IP address**. Useful options:

```powershell
python screenlink_server.py --list-monitors     # see monitor numbers
python screenlink_server.py --pin 4821          # fixed PIN instead of random
```

Allow it through the firewall once (admin PowerShell):

```powershell
netsh advfirewall firewall add rule name="ScreenLink" dir=in action=allow protocol=TCP localport=8765 profile=private
netsh advfirewall firewall add rule name="ScreenLink discovery" dir=in action=allow protocol=UDP localport=8766 profile=private
```

Run it **as Administrator** if you also want to control elevated windows
(Task Manager, installers, admin terminals) from the phone.

Optional single .exe: `pip install pyinstaller` then
`pyinstaller --onefile --name ScreenLinkServer screenlink_server.py`

## 2. Android app

```bash
flutter create screenlink --org com.example --platforms android
cd screenlink
```

Then copy `client/pubspec.yaml` and `client/lib/main.dart` over the generated ones, and edit
`android/app/src/main/AndroidManifest.xml`:

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET"/>
    <application
        android:usesCleartextTraffic="true"
        ... keep the rest as generated ...>
```

(`usesCleartextTraffic` is required: the stream is plain `ws://` on your LAN.)

```bash
flutter pub get
flutter run --release            # phone connected via USB, or:
flutter build apk --release      # build/app/outputs/flutter-apk/app-release.apk
```

## 3. Use it

1. Put phone and PC on the same Wi-Fi (or connect the PC to the phone's hotspot).
2. Start the server, open the app, tap your PC in the list (or type its IP), enter the PIN.
3. Gestures: **tap** = left click · **drag** = click-and-drag · **long-press** = right click ·
   **two-finger drag** = scroll. The round button (top-right) opens keyboard/shortcuts,
   quality presets and screen picker.

## Mirror vs Extend, and up to 3 phones

- In the app menu (round button) pick **Mirror** or **Extend**. The server asks Windows to
  clone or extend the desktop (same as Win+P) and tells every connected phone to refresh.
- **Extend** needs a second display on the PC (a physical monitor or a virtual display driver).
  If none exists you get a notice and nothing changes.
- Up to **3 phones** can connect at once (`--max-clients 1..3`). In Extend mode each phone is
  given a different PC screen when enough screens exist; with fewer screens they share one.
- Don't want phones changing your PC's display mode? Start the server with `--no-topology`.
- Every connected phone can move the mouse and type on the PC, and each one costs extra CPU.

## Making it a real *extended* display

A Python program can capture and stream a screen, but it cannot create a new
monitor – that needs a Windows *indirect display driver* (a signed C++ kernel-side
driver, which is what Spacedesk installs). Until you have one, ScreenLink shows
an existing monitor (mirror). Once a virtual monitor exists in
Windows (Settings → System → Display → "Extend these displays"), ScreenLink lists it
automatically under **Show which PC screen**; pick it and drag windows onto your phone.

## Notes

- Traffic is not encrypted; the PIN stops other people on the network from connecting
  or controlling the PC (5 wrong tries = 1 minute lockout). Use trusted networks only.
- Screens with UAC prompts / lock screen can't be captured by normal apps; the stream
  pauses and resumes afterwards.
- Latency depends on Wi-Fi. 5 GHz + the "Fast" preset is the lowest-latency setup.
=======
# dee-screenlink
Dee ScreenLink – Windows 11 → Android second screen
>>>>>>> 697ea0a807b6f66abbfe05776333d48832e71eeb
