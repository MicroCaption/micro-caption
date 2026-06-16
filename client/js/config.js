// API base URL: localhost dev → explicit port 8765; any proxy/tunnel → same origin
// (Tailscale funnel routes /api/*, /events/*, /webvtt/* etc. to 8765 transparently).
const API = window.MC_API_URL ||
  (window.location.port === '3000' ? 'http://localhost:8765' : window.location.origin);
