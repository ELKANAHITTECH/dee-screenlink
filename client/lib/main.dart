import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

const int kDefaultPort = 8765;
const int kDiscoveryPort = 8766;
const String kDiscoveryMagic = 'SCREENLINK_DISCOVER';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const ScreenLinkApp());
}

class ScreenLinkApp extends StatelessWidget {
  const ScreenLinkApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ScreenLink',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: const Color(0xFF3D7BFF),
        brightness: Brightness.dark,
      ),
      home: const ConnectScreen(),
    );
  }
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------
class ServerInfo {
  final String name;
  final String host;
  final int port;
  ServerInfo(this.name, this.host, this.port);
}

Future<List<ServerInfo>> discoverServers() async {
  final found = <String, ServerInfo>{};
  RawDatagramSocket? socket;
  try {
    socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    socket.broadcastEnabled = true;
    final sock = socket;
    final sub = sock.listen((event) {
      if (event != RawSocketEvent.read) return;
      final dg = sock.receive();
      if (dg == null) return;
      try {
        final m = jsonDecode(utf8.decode(dg.data)) as Map<String, dynamic>;
        found[dg.address.address] = ServerInfo(
          (m['name'] ?? 'PC').toString(),
          dg.address.address,
          (m['port'] as num?)?.toInt() ?? kDefaultPort,
        );
      } catch (_) {}
    });
    final msg = utf8.encode(kDiscoveryMagic);
    for (var i = 0; i < 3; i++) {
      sock.send(msg, InternetAddress('255.255.255.255'), kDiscoveryPort);
      await Future.delayed(const Duration(milliseconds: 500));
    }
    await Future.delayed(const Duration(milliseconds: 400));
    await sub.cancel();
  } catch (_) {
    // ignore: network not available
  } finally {
    socket?.close();
  }
  return found.values.toList();
}

// ---------------------------------------------------------------------------
// Connect screen
// ---------------------------------------------------------------------------
class ConnectScreen extends StatefulWidget {
  const ConnectScreen({super.key});

  @override
  State<ConnectScreen> createState() => _ConnectScreenState();
}

class _ConnectScreenState extends State<ConnectScreen> {
  final _host = TextEditingController();
  final _pin = TextEditingController();
  int _port = kDefaultPort;
  List<ServerInfo> _servers = [];
  bool _scanning = false;

  @override
  void initState() {
    super.initState();
    _loadSaved();
    _scan();
  }

  @override
  void dispose() {
    _host.dispose();
    _pin.dispose();
    super.dispose();
  }

  Future<void> _loadSaved() async {
    final p = await SharedPreferences.getInstance();
    if (!mounted) return;
    _host.text = p.getString('host') ?? '';
  }

  Future<void> _scan() async {
    if (_scanning) return;
    setState(() => _scanning = true);
    final result = await discoverServers();
    if (!mounted) return;
    setState(() {
      _servers = result;
      _scanning = false;
    });
  }

  Future<void> _connect() async {
    var host = _host.text.trim();
    var port = _port;
    if (host.contains(':')) {
      final parts = host.split(':');
      host = parts[0];
      port = int.tryParse(parts[1]) ?? kDefaultPort;
    }
    if (host.isEmpty || _pin.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Enter the PC address and the PIN')),
      );
      return;
    }
    final p = await SharedPreferences.getInstance();
    await p.setString('host', host);
    if (!mounted) return;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ViewerScreen(host: host, port: port, pin: _pin.text.trim()),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('ScreenLink')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Row(
              children: [
                Text('PCs on this network',
                    style: Theme.of(context).textTheme.titleMedium),
                const Spacer(),
                _scanning
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : IconButton(
                        onPressed: _scan,
                        icon: const Icon(Icons.refresh),
                        tooltip: 'Scan again'),
              ],
            ),
            if (_servers.isEmpty && !_scanning)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Text(
                  'No PC found automatically. Make sure the server is running and '
                  'both devices are on the same Wi-Fi, or type the address below.',
                ),
              ),
            for (final s in _servers)
              Card(
                child: ListTile(
                  leading: const Icon(Icons.computer),
                  title: Text(s.name),
                  subtitle: Text('${s.host}:${s.port}'),
                  onTap: () => setState(() {
                    _host.text = s.host;
                    _port = s.port;
                  }),
                ),
              ),
            const SizedBox(height: 16),
            TextField(
              controller: _host,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(
                labelText: 'PC address (e.g. 192.168.1.20)',
                border: OutlineInputBorder(),
              ),
              onChanged: (_) => _port = kDefaultPort,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _pin,
              keyboardType: TextInputType.number,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'PIN shown on the PC',
                border: OutlineInputBorder(),
              ),
              onSubmitted: (_) => _connect(),
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: _connect,
              icon: const Icon(Icons.cast_connected),
              label: const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Text('Connect'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Viewer
// ---------------------------------------------------------------------------
class ViewerScreen extends StatefulWidget {
  final String host;
  final int port;
  final String pin;
  const ViewerScreen({
    super.key,
    required this.host,
    required this.port,
    required this.pin,
  });

  @override
  State<ViewerScreen> createState() => _ViewerScreenState();
}

class _ViewerScreenState extends State<ViewerScreen> {
  static const presets = <Map<String, dynamic>>[
    {'name': 'Fast', 'scale': 0.5, 'quality': 45, 'fps': 20},
    {'name': 'Balanced', 'scale': 0.75, 'quality': 60, 'fps': 30},
    {'name': 'Sharp', 'scale': 1.0, 'quality': 75, 'fps': 30},
  ];

  WebSocketChannel? _ch;
  StreamSubscription? _sub;
  ui.Image? _image;
  String? _status = 'Connecting…';
  bool _error = false;
  bool _closing = false;

  double _aspect = 16 / 9;
  List<Map<String, dynamic>> _monitors = [];
  int _monitorId = 1;
  int _preset = 1;
  String _mode = 'mirror';
  bool _canTopology = false;
  int _clients = 1;
  int _maxClients = 3;
  Size _box = Size.zero;

  // frame pipeline
  final List<Uint8List> _queue = [];
  bool _decoding = false;

  // touch state
  final Map<int, Offset> _ptrs = {};
  Offset _startPos = Offset.zero;
  Offset _lastSingle = Offset.zero;
  Offset _lastMid = Offset.zero;
  bool _dragging = false;
  bool _multi = false;
  bool _longFired = false;
  Timer? _longTimer;
  int _lastMoveMs = 0;

  @override
  void initState() {
    super.initState();
    SystemChrome.setPreferredOrientations(const [
      DeviceOrientation.landscapeLeft,
      DeviceOrientation.landscapeRight,
    ]);
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    WakelockPlus.enable();
    _connect();
  }

  @override
  void dispose() {
    _closing = true;
    _longTimer?.cancel();
    _teardown();
    _image?.dispose();
    WakelockPlus.disable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    SystemChrome.setPreferredOrientations(const []);
    super.dispose();
  }

  // ---- connection ---------------------------------------------------------
  void _teardown() {
    _sub?.cancel();
    _sub = null;
    try {
      _ch?.sink.close();
    } catch (_) {}
    _ch = null;
  }

  Future<void> _connect() async {
    if (!mounted) return;
    setState(() {
      _status = 'Connecting to ${widget.host}…';
      _error = false;
    });
    try {
      final ch = WebSocketChannel.connect(
          Uri.parse('ws://${widget.host}:${widget.port}'));
      _ch = ch;
      await ch.ready.timeout(const Duration(seconds: 8));
      if (!mounted || _closing) return;
      _sub = ch.stream.listen(
        _onData,
        onDone: _onDone,
        onError: (e) => _fail('Connection error: $e'),
      );
      final p = presets[_preset];
      _send({
        'type': 'auth',
        'pin': widget.pin,
        'scale': p['scale'],
        'quality': p['quality'],
        'fps': p['fps'],
      });
    } catch (e) {
      _fail('Could not connect to ${widget.host}.\n'
          'Check the address, that the server is running, and the Windows firewall.');
    }
  }

  void _retry() {
    _teardown();
    _queue.clear();
    _connect();
  }

  void _fail(String message) {
    if (_closing || !mounted) return;
    _teardown();
    setState(() {
      _status = message;
      _error = true;
    });
  }

  void _onDone() {
    if (!_error) _fail('Disconnected from the PC.');
  }

  void _send(Map<String, dynamic> m) {
    try {
      _ch?.sink.add(jsonEncode(m));
    } catch (_) {}
  }

  void _onData(dynamic data) {
    if (data is String) {
      Map<String, dynamic> m;
      try {
        m = jsonDecode(data) as Map<String, dynamic>;
      } catch (_) {
        return;
      }
      switch (m['type']) {
        case 'hello':
          final w = (m['width'] as num).toDouble();
          final h = (m['height'] as num).toDouble();
          setState(() {
            _monitors = (m['monitors'] as List).cast<Map<String, dynamic>>();
            _monitorId = (m['monitor'] as num).toInt();
            if (h > 0) _aspect = w / h;
            _mode = (m['mode'] ?? 'mirror').toString();
            _canTopology = m['can_topology'] == true;
            _clients = (m['clients'] as num?)?.toInt() ?? 1;
            _maxClients = (m['max_clients'] as num?)?.toInt() ?? 3;
            if (_image == null) _status = 'Waiting for first frame…';
          });
          break;
        case 'notice':
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(content: Text(m['message']?.toString() ?? '')),
            );
          }
          break;
        case 'error':
          _fail(m['message']?.toString() ?? 'Server error');
          break;
      }
    } else if (data is List<int>) {
      _queue.add(data is Uint8List ? data : Uint8List.fromList(data));
      if (!_decoding) _drain();
    }
  }

  // Decode only the newest frame; tell the server about every frame we consumed
  // so it can pace itself to what the phone can actually display.
  Future<void> _drain() async {
    _decoding = true;
    while (_queue.isNotEmpty && mounted && !_closing) {
      final bytes = _queue.removeLast();
      final skipped = _queue.length;
      _queue.clear();
      for (var i = 0; i < skipped; i++) {
        _send({'type': 'ack'});
      }
      try {
        final codec = await ui.instantiateImageCodec(bytes);
        final frame = await codec.getNextFrame();
        codec.dispose();
        if (!mounted || _closing) {
          frame.image.dispose();
          break;
        }
        final old = _image;
        setState(() {
          _image = frame.image;
          _status = null;
          _error = false;
        });
        if (old != null) {
          WidgetsBinding.instance.addPostFrameCallback((_) => old.dispose());
        }
      } catch (_) {}
      _send({'type': 'ack'});
    }
    _decoding = false;
  }

  // ---- coordinates --------------------------------------------------------
  Rect _imageRect() {
    final aspect =
        _image != null ? _image!.width / _image!.height : _aspect;
    var w = _box.width;
    var h = w / aspect;
    if (h > _box.height) {
      h = _box.height;
      w = h * aspect;
    }
    return Rect.fromLTWH((_box.width - w) / 2, (_box.height - h) / 2, w, h);
  }

  Offset _norm(Offset local) {
    final r = _imageRect();
    final nx = ((local.dx - r.left) / r.width).clamp(0.0, 1.0).toDouble();
    final ny = ((local.dy - r.top) / r.height).clamp(0.0, 1.0).toDouble();
    return Offset(nx, ny);
  }

  void _mouse(String action, Offset local, {String button = 'left'}) {
    final n = _norm(local);
    _send({
      'type': 'mouse',
      'action': action,
      'x': n.dx,
      'y': n.dy,
      'button': button,
    });
  }

  // ---- touch handling -----------------------------------------------------
  // 1 finger: tap = left click, drag = click-and-drag, long press = right click
  // 2 fingers: scroll (vertical and horizontal)
  Offset _mid() {
    var s = Offset.zero;
    for (final p in _ptrs.values) {
      s += p;
    }
    return s / _ptrs.length.toDouble();
  }

  void _onDown(PointerDownEvent e) {
    if (_image == null) return;
    _ptrs[e.pointer] = e.localPosition;
    if (_ptrs.length == 1) {
      _startPos = e.localPosition;
      _lastSingle = e.localPosition;
      _dragging = false;
      _multi = false;
      _longFired = false;
      _mouse('move', e.localPosition);
      _longTimer?.cancel();
      _longTimer = Timer(const Duration(milliseconds: 550), () {
        if (!_dragging && !_multi && _ptrs.length == 1) {
          _longFired = true;
          HapticFeedback.mediumImpact();
          _mouse('click', _startPos, button: 'right');
        }
      });
    } else {
      _longTimer?.cancel();
      if (_dragging) {
        _mouse('up', _lastSingle);
        _dragging = false;
      }
      _multi = true;
      _lastMid = _mid();
    }
  }

  void _onMove(PointerMoveEvent e) {
    if (!_ptrs.containsKey(e.pointer)) return;
    _ptrs[e.pointer] = e.localPosition;
    if (_multi) {
      final mid = _mid();
      final d = mid - _lastMid;
      _lastMid = mid;
      final dy = (d.dy * 4).round();
      final dx = (-d.dx * 4).round();
      if (dy != 0 || dx != 0) {
        _send({'type': 'mouse', 'action': 'scroll', 'dy': dy, 'dx': dx});
      }
      return;
    }
    _lastSingle = e.localPosition;
    if (!_dragging &&
        !_longFired &&
        (e.localPosition - _startPos).distance > 14) {
      _dragging = true;
      _longTimer?.cancel();
      _mouse('move', _startPos);
      _mouse('down', _startPos);
    }
    if (_dragging) {
      final now = DateTime.now().millisecondsSinceEpoch;
      if (now - _lastMoveMs >= 12) {
        _lastMoveMs = now;
        _mouse('move', e.localPosition);
      }
    }
  }

  void _resetTouch() {
    _dragging = false;
    _multi = false;
    _longFired = false;
  }

  void _onUp(PointerUpEvent e) {
    if (!_ptrs.containsKey(e.pointer)) return;
    _ptrs.remove(e.pointer);
    _longTimer?.cancel();
    if (_ptrs.isEmpty) {
      if (_multi) {
        // scroll gesture ended
      } else if (_dragging) {
        _mouse('up', e.localPosition);
      } else if (!_longFired) {
        _mouse('click', _startPos);
      }
      _resetTouch();
    } else if (_multi) {
      _lastMid = _mid();
    }
  }

  void _onCancel(PointerCancelEvent e) {
    _ptrs.remove(e.pointer);
    _longTimer?.cancel();
    if (_ptrs.isEmpty) {
      if (_dragging) _mouse('up', _lastSingle);
      _resetTouch();
    }
  }

  // ---- menus --------------------------------------------------------------
  void _applyPreset(int i) {
    setState(() => _preset = i);
    final p = presets[i];
    _send({
      'type': 'settings',
      'scale': p['scale'],
      'quality': p['quality'],
      'fps': p['fps'],
    });
  }

  void _openMenu() {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      constraints: const BoxConstraints(maxWidth: 560),
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setSheet) => SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                FilledButton.tonalIcon(
                  onPressed: () {
                    Navigator.pop(ctx);
                    _openKeyboard();
                  },
                  icon: const Icon(Icons.keyboard),
                  label: const Text('Keyboard & shortcuts'),
                ),
                if (_canTopology) ...[
                  const SizedBox(height: 16),
                  const Text('Screen mode'),
                  const SizedBox(height: 6),
                  SegmentedButton<String>(
                    segments: const [
                      ButtonSegment(
                          value: 'mirror',
                          icon: Icon(Icons.screen_share),
                          label: Text('Mirror')),
                      ButtonSegment(
                          value: 'extend',
                          icon: Icon(Icons.add_to_queue),
                          label: Text('Extend')),
                    ],
                    selected: {_mode},
                    onSelectionChanged: (v) {
                      final mode = v.first;
                      if (mode == _mode) return;
                      setState(() => _mode = mode);
                      setSheet(() {});
                      _send({'type': 'settings', 'mode': mode});
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(
                            content: Text(mode == 'extend'
                                ? 'Extending the PC desktop…'
                                : 'Mirroring the PC main screen…')),
                      );
                    },
                  ),
                  const SizedBox(height: 4),
                  const Text(
                    'Extend needs a second (physical or virtual) display on the PC.',
                    style: TextStyle(fontSize: 12, color: Colors.white54),
                  ),
                ],
                const SizedBox(height: 16),
                const Text('Quality'),
                const SizedBox(height: 6),
                Wrap(
                  spacing: 8,
                  children: [
                    for (var i = 0; i < presets.length; i++)
                      ChoiceChip(
                        label: Text(presets[i]['name'] as String),
                        selected: _preset == i,
                        onSelected: (_) {
                          _applyPreset(i);
                          setSheet(() {});
                        },
                      ),
                  ],
                ),
                if (_monitors.length > 1) ...[
                  const SizedBox(height: 16),
                  const Text('Show which PC screen'),
                  const SizedBox(height: 6),
                  Wrap(
                    spacing: 8,
                    children: [
                      for (final m in _monitors)
                        ChoiceChip(
                          label: Text(
                              'Screen ${m['id']} (${m['width']}×${m['height']})'),
                          selected: _monitorId == (m['id'] as num).toInt(),
                          onSelected: (_) {
                            _send({
                              'type': 'settings',
                              'monitor': (m['id'] as num).toInt()
                            });
                            setState(
                                () => _monitorId = (m['id'] as num).toInt());
                            setSheet(() {});
                          },
                        ),
                    ],
                  ),
                ],
                const SizedBox(height: 16),
                Text('Devices connected: $_clients / $_maxClients',
                    style: const TextStyle(fontSize: 12, color: Colors.white54)),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  onPressed: () {
                    Navigator.pop(ctx);
                    Navigator.of(context).pop();
                  },
                  icon: const Icon(Icons.link_off),
                  label: const Text('Disconnect'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  static const _keyButtons = <List<dynamic>>[
    ['Esc', ['esc']],
    ['Tab', ['tab']],
    ['⌫', ['backspace']],
    ['Del', ['delete']],
    ['Enter', ['enter']],
    ['←', ['left']],
    ['↑', ['up']],
    ['↓', ['down']],
    ['→', ['right']],
    ['Home', ['home']],
    ['End', ['end']],
    ['PgUp', ['pageup']],
    ['PgDn', ['pagedown']],
    ['Win', ['win']],
    ['Ctrl+C', ['ctrl', 'c']],
    ['Ctrl+V', ['ctrl', 'v']],
    ['Ctrl+X', ['ctrl', 'x']],
    ['Ctrl+A', ['ctrl', 'a']],
    ['Ctrl+Z', ['ctrl', 'z']],
    ['Alt+Tab', ['alt', 'tab']],
    ['Win+D', ['win', 'd']],
    ['F5', ['f5']],
  ];

  void _sendDiff(String oldText, String newText) {
    var p = 0;
    final max = oldText.length < newText.length ? oldText.length : newText.length;
    while (p < max && oldText.codeUnitAt(p) == newText.codeUnitAt(p)) {
      p++;
    }
    final backspaces = oldText.length - p;
    if (backspaces > 0) {
      _send({
        'type': 'key',
        'keys': ['backspace'],
        'repeat': backspaces,
      });
    }
    final added = newText.substring(p);
    if (added.isNotEmpty) {
      _send({'type': 'text', 'text': added});
    }
  }

  void _openKeyboard() {
    final ctl = TextEditingController();
    var last = '';
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      constraints: const BoxConstraints(maxWidth: 640),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.of(ctx).viewInsets.bottom),
        child: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(12),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: ctl,
                  autofocus: true,
                  autocorrect: false,
                  enableSuggestions: false,
                  textInputAction: TextInputAction.send,
                  decoration: const InputDecoration(
                    hintText: 'Type here – text goes to the PC',
                    border: OutlineInputBorder(),
                  ),
                  onChanged: (v) {
                    _sendDiff(last, v);
                    last = v;
                    if (v.length > 200) {
                      ctl.clear();
                      last = '';
                    }
                  },
                  onSubmitted: (_) {
                    _send({
                      'type': 'key',
                      'keys': ['enter']
                    });
                    ctl.clear();
                    last = '';
                  },
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    for (final k in _keyButtons)
                      OutlinedButton(
                        onPressed: () => _send({
                          'type': 'key',
                          'keys': k[1],
                        }),
                        child: Text(k[0] as String),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    ).whenComplete(ctl.dispose);
  }

  // ---- build --------------------------------------------------------------
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: Stack(
        children: [
          Positioned.fill(
            child: LayoutBuilder(
              builder: (ctx, c) {
                _box = Size(c.maxWidth, c.maxHeight);
                return Listener(
                  behavior: HitTestBehavior.opaque,
                  onPointerDown: _onDown,
                  onPointerMove: _onMove,
                  onPointerUp: _onUp,
                  onPointerCancel: _onCancel,
                  child: _image == null
                      ? const SizedBox.expand()
                      : RawImage(
                          image: _image,
                          width: c.maxWidth,
                          height: c.maxHeight,
                          fit: BoxFit.contain,
                          filterQuality: FilterQuality.medium,
                        ),
                );
              },
            ),
          ),
          if (_status != null)
            Positioned.fill(
              child: Container(
                color: Colors.black87,
                alignment: Alignment.center,
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (!_error)
                      const CircularProgressIndicator()
                    else
                      const Icon(Icons.error_outline,
                          size: 48, color: Colors.redAccent),
                    const SizedBox(height: 16),
                    Text(_status!, textAlign: TextAlign.center),
                    if (_error) ...[
                      const SizedBox(height: 16),
                      Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          FilledButton(
                              onPressed: _retry, child: const Text('Retry')),
                          const SizedBox(width: 12),
                          OutlinedButton(
                              onPressed: () => Navigator.of(context).pop(),
                              child: const Text('Back')),
                        ],
                      ),
                    ],
                  ],
                ),
              ),
            ),
          if (_status == null)
            Positioned(
              top: 6,
              right: 6,
              child: Material(
                color: Colors.black.withAlpha(110),
                shape: const CircleBorder(),
                child: IconButton(
                  onPressed: _openMenu,
                  icon: const Icon(Icons.tune, color: Colors.white70),
                  tooltip: 'Menu',
                ),
              ),
            ),
        ],
      ),
    );
  }
}
