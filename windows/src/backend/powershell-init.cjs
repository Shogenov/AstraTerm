'use strict';

// PSReadLine interprets Ctrl-letter keys using the current keyboard layout and
// can turn a correct Ctrl+C console event into Cyrillic Ctrl+с (#1393).
// Add equivalent bindings only in this child process, without changing the
// user's layout, PowerShell profile, installed modules, or native-app input.
const initializationCommand = String.raw`
& {
  try {
    Import-Module PSReadLine -ErrorAction Stop
    $bindings = @(Get-PSReadLineKeyHandler -Bound -ErrorAction Stop)
    if (-not ('AstraTermConsoleLayouts' -as [type])) {
      Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class AstraTermConsoleLayouts {
  [DllImport("user32.dll")] private static extern int GetKeyboardLayoutList(int count, [Out] IntPtr[] layouts);
  [DllImport("user32.dll")] private static extern uint MapVirtualKeyEx(uint key, uint kind, IntPtr layout);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int ToUnicodeEx(uint key, uint scan, byte[] state, [Out] char[] text, int length, uint flags, IntPtr layout);
  public static string[] Letters(uint key) {
    int count = Math.Min(256, Math.Max(0, GetKeyboardLayoutList(0, null)));
    var layouts = new IntPtr[count];
    GetKeyboardLayoutList(count, layouts);
    var values = new HashSet<string>();
    foreach (var layout in layouts) {
      var text = new char[4];
      int result = ToUnicodeEx(key, MapVirtualKeyEx(key, 0, layout), new byte[256], text, text.Length, 4, layout);
      if (result == 1 && text[0] > 127 && !char.IsControl(text[0])) values.Add(text[0].ToString());
    }
    var output = new string[values.Count]; values.CopyTo(output); return output;
  }
}
'@ | Out-Null
    }
    foreach ($code in 65..90) {
      $chord = 'Ctrl+' + ([char]$code).ToString().ToLowerInvariant()
      $original = $bindings | Where-Object { $_.Key -ceq $chord } | Select-Object -First 1
      if (-not $original -or $original.Function -eq 'CustomAction' -or $original.Function -eq 'SelfInsert') { continue }
      foreach ($letter in [AstraTermConsoleLayouts]::Letters($code)) {
        try {
          $alias = 'Ctrl+' + $letter
          if (-not ($bindings | Where-Object { $_.Key -ceq $alias })) {
            Set-PSReadLineKeyHandler -Chord $alias -Function $original.Function -ErrorAction Stop
          }
        } catch { }
      }
    }
  } catch { }
}
`;

module.exports = { initializationCommand };
