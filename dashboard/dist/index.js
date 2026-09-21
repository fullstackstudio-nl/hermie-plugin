// Hermie ships no dashboard UI: its client is the Hermie app, which talks to
// /api/plugins/hermie/ directly. The dashboard fetches a plugin's entry
// whatever its manifest says, so this exists to be a successful, empty answer
// rather than a 404 in somebody's console. The manifest hides the tab.
export default {};
