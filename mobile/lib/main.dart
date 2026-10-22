// Local AI - phone app.
//
// The AI runs on your PC. This app remembers where your PC is (home Wi-Fi
// address and, optionally, a Tailscale address for when you're away), picks
// whichever one answers, and shows the Local AI app full-screen. The PIN
// login, chat, search, code, files and security screens all come from the PC.

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

const bg = Color(0xFF0F1115);
const panel = Color(0xFF171A21);
const accent = Color(0xFF7CC4FF);

void main() => runApp(const LocalAIApp());

class LocalAIApp extends StatelessWidget {
  const LocalAIApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Local AI',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: bg,
        colorScheme: const ColorScheme.dark(primary: accent, surface: panel),
        useMaterial3: true,
      ),
      home: const StartScreen(),
    );
  }
}

/// Turns "192.168.1.5" or "192.168.1.5:8790" into "http://192.168.1.5:8790".
String normalizeAddress(String input) {
  var s = input.trim();
  if (s.isEmpty) return s;
  if (!s.startsWith('http://') && !s.startsWith('https://')) s = 'http://$s';
  final uri = Uri.tryParse(s);
  if (uri == null || uri.host.isEmpty) return '';
  final port = uri.hasPort ? uri.port : 8790;
  return '${uri.scheme}://${uri.host}:$port';
}

/// True if a Local AI server answers at [base].
Future<bool> reachable(String base, {Duration timeout = const Duration(seconds: 3)}) async {
  if (base.isEmpty) return false;
  try {
    final r = await http.get(Uri.parse('$base/api/status')).timeout(timeout);
    return r.statusCode == 200 && r.body.contains('"version"');
  } catch (_) {
    return false;
  }
}

class StartScreen extends StatefulWidget {
  const StartScreen({super.key});

  @override
  State<StartScreen> createState() => _StartScreenState();
}

class _StartScreenState extends State<StartScreen> {
  final home = TextEditingController();
  final away = TextEditingController();
  String status = 'Looking for your PC...';
  bool busy = true;
  bool showSetup = false;

  @override
  void initState() {
    super.initState();
    _autoConnect();
  }

  Future<void> _autoConnect() async {
    final prefs = await SharedPreferences.getInstance();
    home.text = prefs.getString('home') ?? '';
    away.text = prefs.getString('away') ?? '';
    if (home.text.isEmpty && away.text.isEmpty) {
      setState(() {
        busy = false;
        showSetup = true;
        status = '';
      });
      return;
    }
    await _connect(save: false);
  }

  Future<void> _connect({bool save = true}) async {
    final h = normalizeAddress(home.text);
    final a = normalizeAddress(away.text);
    if (h.isEmpty && a.isEmpty) {
      setState(() => status = 'Enter the address shown in Local AI on your PC (Settings > Use on your phone).');
      return;
    }
    setState(() {
      busy = true;
      status = 'Connecting...';
    });
    if (save) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('home', h);
      await prefs.setString('away', a);
    }
    for (final base in [h, a]) {
      if (base.isNotEmpty && await reachable(base)) {
        if (!mounted) return;
        await Navigator.of(context).push(MaterialPageRoute(builder: (_) => AppScreen(base: base)));
        if (!mounted) return;
        setState(() {
          busy = false;
          showSetup = true;
          status = '';
        });
        return;
      }
    }
    setState(() {
      busy = false;
      showSetup = true;
      status = "Couldn't reach your PC. Make sure Local AI is running on it, and that you're on the "
          "same Wi-Fi (or Tailscale is on for the away address).";
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(18),
                    child: Image.asset('assets/icon.png', width: 72, height: 72),
                  ),
                  const SizedBox(height: 14),
                  const Text('Local AI',
                      textAlign: TextAlign.center, style: TextStyle(fontSize: 26, fontWeight: FontWeight.w700)),
                  const SizedBox(height: 6),
                  const Text('Your private AI, running on your PC',
                      textAlign: TextAlign.center, style: TextStyle(color: Colors.white60)),
                  const SizedBox(height: 28),
                  if (showSetup) ...[
                    _field(home, 'Home Wi-Fi address', 'e.g. 192.168.1.20:8790'),
                    const SizedBox(height: 12),
                    _field(away, 'Away address (Tailscale, optional)', 'e.g. 100.x.y.z:8790'),
                    const SizedBox(height: 8),
                    const Text(
                      'Find these in Local AI on your PC: Settings (gear) > Use on your phone. '
                      "You'll enter your PIN on the next screen.",
                      style: TextStyle(color: Colors.white54, fontSize: 13),
                    ),
                    const SizedBox(height: 18),
                    FilledButton(
                      onPressed: busy ? null : () => _connect(),
                      style: FilledButton.styleFrom(padding: const EdgeInsets.symmetric(vertical: 14)),
                      child: const Text('Connect'),
                    ),
                  ],
                  if (busy) ...[
                    const SizedBox(height: 18),
                    const Center(child: CircularProgressIndicator()),
                  ],
                  if (status.isNotEmpty) ...[
                    const SizedBox(height: 14),
                    Text(status, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70)),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _field(TextEditingController c, String label, String hint) => TextField(
        controller: c,
        keyboardType: TextInputType.url,
        autocorrect: false,
        decoration: InputDecoration(
          labelText: label,
          hintText: hint,
          filled: true,
          fillColor: panel,
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        ),
      );
}

class AppScreen extends StatefulWidget {
  final String base;
  const AppScreen({super.key, required this.base});

  @override
  State<AppScreen> createState() => _AppScreenState();
}

class _AppScreenState extends State<AppScreen> {
  InAppWebViewController? web;
  String? error;
  double progress = 0;

  Future<void> _openOutside(Uri uri) async {
    if (!await launchUrl(uri, mode: LaunchMode.externalApplication)) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text("Couldn't open $uri")));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) async {
        if (didPop) return;
        if (web != null && await web!.canGoBack()) {
          await web!.goBack();
        } else if (context.mounted) {
          Navigator.of(context).pop();
        }
      },
      child: Scaffold(
        body: SafeArea(
          bottom: false,
          child: Stack(
            children: [
              InAppWebView(
                initialUrlRequest: URLRequest(url: WebUri(widget.base)),
                initialSettings: InAppWebViewSettings(
                  javaScriptEnabled: true,
                  useOnDownloadStart: true,
                  useShouldOverrideUrlLoading: true,
                  mediaPlaybackRequiresUserGesture: false,
                  allowsInlineMediaPlayback: true,
                  supportZoom: false,
                  transparentBackground: true,
                  mixedContentMode: MixedContentMode.MIXED_CONTENT_ALWAYS_ALLOW,
                ),
                onWebViewCreated: (c) => web = c,
                onProgressChanged: (c, p) => setState(() => progress = p / 100),
                onLoadStop: (c, url) => setState(() => error = null),
                onReceivedError: (c, request, err) {
                  if (request.isForMainFrame ?? true) {
                    setState(() => error = "Lost the connection to your PC.\n${err.description}");
                  }
                },
                // Links to other websites (search sources etc.) open in the phone's browser.
                shouldOverrideUrlLoading: (c, action) async {
                  final uri = action.request.url;
                  if (uri != null && uri.host.isNotEmpty && uri.host != Uri.parse(widget.base).host) {
                    await _openOutside(uri);
                    return NavigationActionPolicy.CANCEL;
                  }
                  return NavigationActionPolicy.ALLOW;
                },
                // File downloads (short-lived links from the PC) go to the phone's browser to save.
                onDownloadStartRequest: (c, req) async => _openOutside(req.url),
              ),
              if (progress < 1) LinearProgressIndicator(value: progress, minHeight: 2),
              if (error != null)
                Container(
                  color: bg,
                  padding: const EdgeInsets.all(24),
                  alignment: Alignment.center,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.wifi_off, size: 48, color: Colors.white54),
                      const SizedBox(height: 12),
                      Text(error!, textAlign: TextAlign.center),
                      const SizedBox(height: 16),
                      Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                        FilledButton(onPressed: () => web?.reload(), child: const Text('Try again')),
                        const SizedBox(width: 10),
                        OutlinedButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Change PC address')),
                      ]),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
