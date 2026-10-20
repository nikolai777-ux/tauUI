Online models - one .json file each (made by the Library's + Model > Online...; edit freely).

{
  "provider": "claude_code",          the service: claude_code = Claude Code on your Claude plan
  "model": "claude-sonnet-5-5",       the service's model id ("" = the service's own default)
  "label": "Claude Sonnet 5.5",       how the app lists it
  "settings": {                       read each time the model is used:
    "instructions": "F:/.../my.txt"   claude_code: the brain's instructions (default prompts/executor.txt)
  }
}
