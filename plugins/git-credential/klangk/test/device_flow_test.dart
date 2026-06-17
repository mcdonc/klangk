import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_git_credential/github_device_flow.dart';

const _baseUrl = 'http://localhost:8000';

void main() {
  group('requestDeviceCode', () {
    test('parses success response', () async {
      final client = MockClient((request) async {
        expect(
          request.url.toString(),
          '$_baseUrl/api/github/device/code',
        );
        expect(request.method, 'POST');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['client_id'], 'test-client-id');
        expect(body['scope'], 'repo');
        return http.Response(
          jsonEncode({
            'device_code': 'dev123',
            'user_code': 'ABCD-1234',
            'verification_uri': 'https://github.com/login/device',
            'interval': 5,
            'expires_in': 900,
          }),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.requestDeviceCode();

      expect(result.deviceCode, 'dev123');
      expect(result.userCode, 'ABCD-1234');
      expect(result.verificationUri, 'https://github.com/login/device');
      expect(result.interval, 5);
      expect(result.expiresIn, 900);
    });

    test('defaults interval to 5 when not provided', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({
            'device_code': 'dev123',
            'user_code': 'ABCD-1234',
            'verification_uri': 'https://github.com/login/device',
            'expires_in': 900,
          }),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.requestDeviceCode();
      expect(result.interval, 5);
    });

    test('throws on HTTP error', () async {
      final client = MockClient((request) async {
        return http.Response('Server Error', 500);
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      expect(
        () => flow.requestDeviceCode(),
        throwsA(isA<GitHubDeviceFlowException>()),
      );
    });

    test('throws on GitHub error response', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({
            'error': 'unauthorized_client',
            'error_description': 'The client is not authorized',
          }),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      expect(
        () => flow.requestDeviceCode(),
        throwsA(
          isA<GitHubDeviceFlowException>().having(
            (e) => e.message,
            'message',
            'The client is not authorized',
          ),
        ),
      );
    });
  });

  group('pollForToken', () {
    test('returns success with access token', () async {
      final client = MockClient((request) async {
        expect(
          request.url.toString(),
          '$_baseUrl/api/github/device/token',
        );
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['client_id'], 'test-client-id');
        expect(body['device_code'], 'dev123');
        expect(
          body['grant_type'],
          'urn:ietf:params:oauth:grant-type:device_code',
        );
        return http.Response(
          jsonEncode({
            'access_token': 'gho_abc123',
            'token_type': 'bearer',
            'scope': 'repo',
          }),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.pollForToken('dev123');

      expect(result.status, DeviceFlowStatus.success);
      expect(result.accessToken, 'gho_abc123');
    });

    test('returns pending status', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({'error': 'authorization_pending'}),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.pollForToken('dev123');

      expect(result.status, DeviceFlowStatus.pending);
      expect(result.accessToken, isNull);
    });

    test('returns slow_down status', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({'error': 'slow_down'}),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.pollForToken('dev123');

      expect(result.status, DeviceFlowStatus.slowDown);
    });

    test('returns expired status', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({'error': 'expired_token'}),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.pollForToken('dev123');

      expect(result.status, DeviceFlowStatus.expired);
    });

    test('returns denied status', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({'error': 'access_denied'}),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      final result = await flow.pollForToken('dev123');

      expect(result.status, DeviceFlowStatus.denied);
    });

    test('throws on HTTP error', () async {
      final client = MockClient((request) async {
        return http.Response('Bad Gateway', 502);
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      expect(
        () => flow.pollForToken('dev123'),
        throwsA(isA<GitHubDeviceFlowException>()),
      );
    });

    test('throws on unknown error', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({
            'error': 'unsupported_grant_type',
            'error_description': 'Grant type not supported',
          }),
          200,
        );
      });

      final flow = GitHubDeviceFlow('test-client-id', _baseUrl, client: client);
      expect(
        () => flow.pollForToken('dev123'),
        throwsA(
          isA<GitHubDeviceFlowException>().having(
            (e) => e.message,
            'message',
            'Grant type not supported',
          ),
        ),
      );
    });
  });
}
