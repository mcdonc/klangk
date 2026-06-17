import 'dart:convert';

import 'package:http/http.dart' as http;

/// Response from GitHub's device code request.
class DeviceCodeResponse {
  final String deviceCode;
  final String userCode;
  final String verificationUri;
  final int interval;
  final int expiresIn;

  DeviceCodeResponse({
    required this.deviceCode,
    required this.userCode,
    required this.verificationUri,
    required this.interval,
    required this.expiresIn,
  });
}

/// Status of a device flow token poll.
enum DeviceFlowStatus { pending, slowDown, expired, denied, success }

/// Result of polling for a device flow token.
class DeviceFlowPollResult {
  final DeviceFlowStatus status;
  final String? accessToken;

  DeviceFlowPollResult(this.status, [this.accessToken]);
}

/// GitHub OAuth device flow client.
///
/// Uses the device authorization grant to obtain an access token without
/// needing a client secret. Safe to use from browser-side code.
class GitHubDeviceFlow {
  final String clientId;
  final http.Client _client;
  final bool _ownsClient;

  GitHubDeviceFlow(this.clientId, {http.Client? client})
      : _client = client ?? http.Client(),
        _ownsClient = client == null;

  void close() {
    if (_ownsClient) _client.close();
  }

  /// Request a device code from GitHub.
  Future<DeviceCodeResponse> requestDeviceCode() async {
    final response = await _client.post(
      Uri.parse('https://github.com/login/device/code'),
      headers: {'Accept': 'application/json'},
      body: {'client_id': clientId, 'scope': 'repo'},
    );

    if (response.statusCode != 200) {
      throw GitHubDeviceFlowException(
        'Failed to request device code: HTTP ${response.statusCode}',
      );
    }

    final data = jsonDecode(response.body) as Map<String, dynamic>;

    if (data.containsKey('error')) {
      throw GitHubDeviceFlowException(
        data['error_description'] as String? ?? data['error'] as String,
      );
    }

    return DeviceCodeResponse(
      deviceCode: data['device_code'] as String,
      userCode: data['user_code'] as String,
      verificationUri: data['verification_uri'] as String,
      interval: data['interval'] as int? ?? 5,
      expiresIn: data['expires_in'] as int? ?? 900,
    );
  }

  /// Poll GitHub for the access token. Call this every [interval] seconds.
  Future<DeviceFlowPollResult> pollForToken(String deviceCode) async {
    final response = await _client.post(
      Uri.parse('https://github.com/login/oauth/access_token'),
      headers: {'Accept': 'application/json'},
      body: {
        'client_id': clientId,
        'device_code': deviceCode,
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
      },
    );

    if (response.statusCode != 200) {
      throw GitHubDeviceFlowException(
        'Failed to poll for token: HTTP ${response.statusCode}',
      );
    }

    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final error = data['error'] as String?;

    if (error == null) {
      final token = data['access_token'] as String?;
      if (token != null) {
        return DeviceFlowPollResult(DeviceFlowStatus.success, token);
      }
    }

    switch (error) {
      case 'authorization_pending':
        return DeviceFlowPollResult(DeviceFlowStatus.pending);
      case 'slow_down':
        return DeviceFlowPollResult(DeviceFlowStatus.slowDown);
      case 'expired_token':
        return DeviceFlowPollResult(DeviceFlowStatus.expired);
      case 'access_denied':
        return DeviceFlowPollResult(DeviceFlowStatus.denied);
      default:
        throw GitHubDeviceFlowException(
          data['error_description'] as String? ?? error ?? 'unknown error',
        );
    }
  }
}

class GitHubDeviceFlowException implements Exception {
  final String message;
  GitHubDeviceFlowException(this.message);

  @override
  String toString() => 'GitHubDeviceFlowException: $message';
}
