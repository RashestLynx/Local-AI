import 'package:flutter_test/flutter_test.dart';
import 'package:local_ai/main.dart';

void main() {
  test('addresses are normalized', () {
    expect(normalizeAddress('192.168.1.5'), 'http://192.168.1.5:8790');
    expect(normalizeAddress('192.168.1.5:9000'), 'http://192.168.1.5:9000');
    expect(normalizeAddress(' http://100.64.0.2:8790 '), 'http://100.64.0.2:8790');
    expect(normalizeAddress(''), '');
  });

  test('unreachable address is reported as not reachable', () async {
    expect(await reachable(''), isFalse);
    expect(await reachable('http://127.0.0.1:1', timeout: const Duration(milliseconds: 500)), isFalse);
  });
}
