Output docs/assets/e2e-demo.gif

Require uv
Require docker

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 18
Set Width 1280
Set Height 760
Set Padding 28
Set Margin 24
Set MarginFill "#050708"
Set BorderRadius 16
Set WindowBar "Rings"
Set WindowBarSize 38
Set Framerate 14
Set PlaybackSpeed 1.5
Set TypingSpeed 20ms
Set Theme {"background":"#0a0e10","foreground":"#e2e8eb","cursor":"#f7ba4a","selection":"#25343a","black":"#0a0e10","brightBlack":"#66757c","red":"#ef6b73","brightRed":"#ff8990","green":"#6ad78e","brightGreen":"#8be5a8","yellow":"#f7ba4a","brightYellow":"#ffd172","blue":"#62a8e8","brightBlue":"#87c1f2","magenta":"#c48ce8","brightMagenta":"#d8a9f2","cyan":"#4dd6dd","brightCyan":"#75e5e9","white":"#e2e8eb","brightWhite":"#ffffff"}

Type "./scripts/readme_demo.sh version"
Enter
Sleep 2s

Type "./scripts/readme_demo.sh sandbox"
Enter
Sleep 4s

Type "./scripts/readme_demo.sh scan"
Enter
Sleep 45s
