/// Web build: this client is the SPA whose sessions carry the DPoP bind
/// deadline (#3230). `web_client.dart` wraps the const with the test
/// seam; import that, not this.
const bool kWebClient = true;
