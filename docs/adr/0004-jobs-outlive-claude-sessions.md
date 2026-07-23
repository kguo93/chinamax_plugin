# Jobs outlive Claude sessions — no SessionEnd reaping

The Codex plugin's SessionEnd hook kills still-running jobs and deletes their records; we deliberately ship no SessionEnd hook at all. Jobs exist for indefinite autonomous work (the acceptance test is a 70+ minute run), so nothing about a Claude session ending may touch a running worker. New sessions learn about inherited Jobs through the SessionStart digest instead.
