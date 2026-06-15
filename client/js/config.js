// API base URL — override with MC_API_URL env var at serve time, or change here for dev.
// In Docker Compose behind nginx this should be '' (same origin).
const API = window.MC_API_URL || 'http://localhost:8765';
