Output docs/assets/team-status.gif

Require claude
Require jq

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 20
Set Width 1120
Set Height 500
Set Padding 28
Set Margin 24
Set MarginFill "#050708"
Set BorderRadius 16
Set WindowBar "Rings"
Set WindowBarSize 38
Set Framerate 14
Set TypingSpeed 20ms
Set Theme {"background":"#0a0e10","foreground":"#e2e8eb","cursor":"#f7ba4a","selection":"#25343a","black":"#0a0e10","brightBlack":"#66757c","red":"#ef6b73","brightRed":"#ff8990","green":"#6ad78e","brightGreen":"#8be5a8","yellow":"#f7ba4a","brightYellow":"#ffd172","blue":"#62a8e8","brightBlue":"#87c1f2","magenta":"#c48ce8","brightMagenta":"#d8a9f2","cyan":"#4dd6dd","brightCyan":"#75e5e9","white":"#e2e8eb","brightWhite":"#ffffff"}

Type "claude auth status --json | jq '{loggedIn, authMethod, apiProvider, subscriptionType}'"
Enter
Sleep 5s
Type "printf '\n✓ official login · ✓ first-party · ✓ Team subscription\n'"
Enter
Sleep 5s
