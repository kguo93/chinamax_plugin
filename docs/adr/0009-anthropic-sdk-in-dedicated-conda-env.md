# Official anthropic SDK in a dedicated conda env

The Runtime is Python 3.12 in a fresh conda env `chinamax` (never the shared py_automation env) and speaks to every Profile through the official `anthropic` SDK pointed at the profile's base URL — native streaming and tool-use blocks instead of ~300 lines of hand-rolled wire/SSE code. Compatibility is considered proven because the same endpoints already serve Claude Code itself in the implement-handoff skill.
