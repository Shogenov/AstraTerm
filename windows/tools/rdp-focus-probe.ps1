param([int]$HelperProcessId,[switch]$AcceptCertificate,[string]$ExpectedCertificateName,[switch]$Quiet,[string]$CapturePath,[long]$ParentHandle,[long]$SurfaceHandle,[switch]$OnlyCapture,[switch]$FocusProbe,[ValidateRange(0,100)][int]$FocusCycles=12)
# Integration probe for owned AstraTerm RDP HWNDs. Never execute remote commands.
# Focus cycles do not cover hover. Test slow boundary crossings manually from
# the host Start menu; instantaneous pointer jumps missed the user's regression.
$inputContract=Get-Content -LiteralPath (Join-Path $PSScriptRoot '../../shared/contracts/rdp.json') -Raw | ConvertFrom-Json
if(-not $PSBoundParameters.ContainsKey('FocusCycles')){$FocusCycles=[int]$inputContract.native_input.focus_cycles}
Add-Type @'
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public class RdpInspect {
 public delegate bool Callback(IntPtr h,IntPtr p);
 [DllImport("user32.dll")]public static extern bool EnumWindows(Callback cb,IntPtr p);
 [DllImport("user32.dll")]public static extern bool EnumChildWindows(IntPtr h,Callback cb,IntPtr p);
 [DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetClassName(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")]public static extern IntPtr SendMessage(IntPtr h,int msg,IntPtr w,IntPtr l);
 [DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")]public static extern IntPtr GetAncestor(IntPtr h,uint flags);
 [DllImport("user32.dll")]public static extern IntPtr GetWindow(IntPtr h,uint command);
 [DllImport("user32.dll")]public static extern void keybd_event(byte key,byte scan,uint flags,UIntPtr extra);
 [StructLayout(LayoutKind.Sequential)]public struct Point{public int X,Y;} [DllImport("user32.dll")]public static extern IntPtr WindowFromPoint(Point point); [DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y); [DllImport("user32.dll")]public static extern void mouse_event(uint flags,uint x,uint y,uint data,UIntPtr extra);
 [StructLayout(LayoutKind.Sequential)]public struct Rect{public int Left,Top,Right,Bottom;}
 [DllImport("user32.dll")]public static extern bool GetWindowRect(IntPtr h,out Rect rect); [DllImport("user32.dll")]public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags); [DllImport("user32.dll")]public static extern bool SetWindowPos(IntPtr hwnd,IntPtr after,int x,int y,int w,int h,uint flags);
 [DllImport("dwmapi.dll")]public static extern int DwmGetWindowAttribute(IntPtr hwnd,int attr,out int value,int size); [DllImport("user32.dll")]static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access); [DllImport("user32.dll")]static extern IntPtr GetThreadDesktop(uint id); [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern bool GetUserObjectInformation(IntPtr h,int idx,StringBuilder value,uint n,out uint needed); public static string Desktop(bool input){uint n;var s=new StringBuilder(128);var h=input?OpenInputDesktop(0,false,1):GetThreadDesktop(GetCurrentThreadId());GetUserObjectInformation(h,2,s,256,out n);return s.ToString();} public static string Text(IntPtr h){var s=new StringBuilder(4096);GetWindowText(h,s,s.Capacity);return s.ToString();}
 [StructLayout(LayoutKind.Sequential)]public struct GuiThread{public int Size,Flags;public IntPtr Active,Focus,Capture,Menu,Move,Caret;public Rect CaretRect;} [DllImport("user32.dll")]public static extern bool GetGUIThreadInfo(uint thread,ref GuiThread info); public static string Class(IntPtr h){var s=new StringBuilder(128);GetClassName(h,s,s.Capacity);return s.ToString();}
 [DllImport("kernel32.dll")]static extern uint GetCurrentThreadId(); [DllImport("user32.dll")]static extern bool AttachThreadInput(uint a,uint b,bool attach); public static void Activate(IntPtr hwnd){uint p;uint other=GetWindowThreadProcessId(GetForegroundWindow(),out p), current=GetCurrentThreadId();bool attached=AttachThreadInput(current,other,true);try{SetForegroundWindow(hwnd);}finally{if(attached)AttachThreadInput(current,other,false);}} public static List<IntPtr> Windows(int pid){var all=new List<IntPtr>();EnumWindows((h,p)=>{uint id;GetWindowThreadProcessId(h,out id);if(id==pid)all.Add(h);return true;},IntPtr.Zero);return all;}
 public static List<IntPtr> Children(IntPtr root){var all=new List<IntPtr>();EnumChildWindows(root,(h,p)=>{all.Add(h);return true;},IntPtr.Zero);return all;}
}
'@
foreach($handle in [RdpInspect]::Windows($HelperProcessId)) {
 if(-not [RdpInspect]::IsWindowVisible($handle)){continue}
 if(-not $Quiet){Write-Output ([RdpInspect]::Class($handle)+' '+[RdpInspect]::Text($handle))}
 $details=([RdpInspect]::Children($handle) | ForEach-Object {[RdpInspect]::Text($_)}) -join "`n"
 foreach($child in [RdpInspect]::Children($handle)) {
  $kind=[RdpInspect]::Class($child);$label=[RdpInspect]::Text($child)
  if($kind -eq 'Edit'){continue}
  if(-not $Quiet){Write-Output ($child.ToInt64().ToString()+' '+$kind+' '+$label)}
  if($AcceptCertificate -and $ExpectedCertificateName -and $details.Contains($ExpectedCertificateName) -and $kind -eq 'Button' -and ($label -match '^&?(Да|Yes)$')) {
   # Explicit developer acceptance of the inspected test host's native certificate warning.
   [RdpInspect]::SetForegroundWindow($handle) | Out-Null
   [RdpInspect]::SendMessage($child,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero) | Out-Null
  }
 }
}
if($OnlyCapture -and $CapturePath -and $ParentHandle){
 Add-Type -AssemblyName System.Drawing,System.Windows.Forms
 $probe=$null
 if($FocusProbe){
  $probe=[Windows.Forms.Form]::new();$probe.Text='AstraTerm focus regression';$probe.Size=[Drawing.Size]::new(300,180);$probe.StartPosition='Manual';$probe.Location=[Drawing.Point]::new(10,10)
  $probe.Show();[Windows.Forms.Application]::DoEvents();[RdpInspect]::Activate($probe.Handle);Start-Sleep -Milliseconds 500
 }
 $owner=[IntPtr]::new($ParentHandle)
 [RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-1),0,0,0,0,0x13) | Out-Null
 $frames=@()
 if($FocusProbe -and $SurfaceHandle -and $FocusCycles){
  $viewRect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect([IntPtr]::new($SurfaceHandle),[ref]$viewRect) | Out-Null
  for($cycle=0;$cycle -lt $FocusCycles;$cycle++){
   foreach($phase in @('blur','restore')){
    if($phase -eq 'blur'){[RdpInspect]::Activate($probe.Handle)}else{[RdpInspect]::Activate($owner)}
    Start-Sleep -Milliseconds 160
    $frame=[Drawing.Bitmap]::new($viewRect.Right-$viewRect.Left,$viewRect.Bottom-$viewRect.Top);$drawing=[Drawing.Graphics]::FromImage($frame)
    try{
     $drawing.CopyFromScreen($viewRect.Left,$viewRect.Top,0,0,$frame.Size)
     $bright=0;$samples=0
     for($x=20;$x -lt $frame.Width-20;$x+=19){for($y=20;$y -lt $frame.Height-20;$y+=19){$pixel=$frame.GetPixel($x,$y);$samples++;if([Math]::Max($pixel.R,[Math]::Max($pixel.G,$pixel.B)) -gt 45){$bright++}}}
     $ratio=$bright/[double]$samples;$frames+=@{cycle=$cycle;phase=$phase;visibleFraction=$ratio}
     if($ratio -lt .05){
      $frame.Save(($CapturePath+'.black.png'),[Drawing.Imaging.ImageFormat]::Png)
      $frames | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+'.cycles.json')
      $ownerRect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect($owner,[ref]$ownerRect) | Out-Null
      $currentView=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect([IntPtr]::new($SurfaceHandle),[ref]$currentView) | Out-Null
      $nativeChildren=@([RdpInspect]::Children([IntPtr]::new($SurfaceHandle)) | ForEach-Object { @{class=[RdpInspect]::Class($_);visible=[RdpInspect]::IsWindowVisible($_)} })
      $previous=[RdpInspect]::GetWindow([IntPtr]::new($SurfaceHandle),3)
      @{phase=$phase;cycle=$cycle;previousSibling=$previous.ToInt64();previousClass=[RdpInspect]::Class($previous);previousVisible=[RdpInspect]::IsWindowVisible($previous);ownerVisible=[RdpInspect]::IsWindowVisible($owner);ownerRect=$ownerRect;viewRect=$currentView;originalViewRect=$viewRect;foreground=[RdpInspect]::Class([RdpInspect]::GetForegroundWindow());children=$nativeChildren} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath ($CapturePath+'.black.json')
      throw 'RDP desktop became black during focus cycling.'
     }
    }finally{$drawing.Dispose();$frame.Dispose()}
   }
  }
  $frames | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+'.cycles.json')
  [RdpInspect]::Activate($probe.Handle)
 }
 Start-Sleep -Milliseconds 500
 $rect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect($owner,[ref]$rect) | Out-Null
 $bitmap=[Drawing.Bitmap]::new($rect.Right-$rect.Left,$rect.Bottom-$rect.Top);$graphics=[Drawing.Graphics]::FromImage($bitmap)
 try{$graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$bitmap.Size);$bitmap.Save($CapturePath,[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$bitmap.Dispose();[RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-2),0,0,0,0,0x13) | Out-Null}
 $foregroundPid=0;[RdpInspect]::GetWindowThreadProcessId([RdpInspect]::GetForegroundWindow(),[ref]$foregroundPid) | Out-Null
 @{foregroundIsOwner=([RdpInspect]::GetAncestor([RdpInspect]::GetForegroundWindow(),2) -eq $owner);foregroundIsProbe=($FocusProbe -and $foregroundPid -eq $PID)} | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+'.state.json')
 if($probe){
  [RdpInspect]::keybd_event(0x5B,0x5B,1,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x5B,0x5B,3,[UIntPtr]::Zero);Start-Sleep -Milliseconds 600
  $localWin=([RdpInspect]::GetForegroundWindow() -ne $probe.Handle -and [RdpInspect]::GetAncestor([RdpInspect]::GetForegroundWindow(),2) -ne $owner)
  @{winHandledLocallyOutsideRdp=$localWin} | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+'.local-key.json')
  [RdpInspect]::keybd_event(0x1B,1,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x1B,1,2,[UIntPtr]::Zero)
  if(-not $localWin){throw 'Windows key was not released to the local shell outside RDP.'}
 }
 if($probe){$probe.Close();$probe.Dispose()}
 return
}
if($CapturePath -and $ParentHandle){
 Add-Type -AssemblyName System.Drawing,System.Windows.Forms
 $owner=[IntPtr]::new($ParentHandle);$cloaked=0;[RdpInspect]::DwmGetWindowAttribute($owner,14,[ref]$cloaked,4) | Out-Null;@{inputDesktop=[RdpInspect]::Desktop($true);threadDesktop=[RdpInspect]::Desktop($false);cloaked=$cloaked;foregroundClass=[RdpInspect]::Class([RdpInspect]::GetForegroundWindow())} | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+".json")
 [RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-1),0,0,0,0,0x13) | Out-Null
 [RdpInspect]::Activate($owner)
 Start-Sleep -Milliseconds 300
 $rect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect($owner,[ref]$rect) | Out-Null
 $bitmap=[Drawing.Bitmap]::new($rect.Right-$rect.Left,$rect.Bottom-$rect.Top);$graphics=[Drawing.Graphics]::FromImage($bitmap)
 try{$graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$bitmap.Size);$bitmap.Save(($CapturePath -replace 'input.png$','focus-restored.png'),[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$bitmap.Dispose()}
 if($SurfaceHandle){
  $viewRect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect([IntPtr]::new($SurfaceHandle),[ref]$viewRect) | Out-Null
  $point=[RdpInspect+Point]::new();$point.X=$viewRect.Left+($viewRect.Right-$viewRect.Left)/2;$point.Y=$viewRect.Top+($viewRect.Bottom-$viewRect.Top)/2
  $actual=[RdpInspect]::WindowFromPoint($point);$actualProcess=0;[RdpInspect]::GetWindowThreadProcessId($actual,[ref]$actualProcess) | Out-Null
  if($actualProcess -ne $HelperProcessId){throw 'The RDP child must own the input hit target.'}
  [RdpInspect]::SetCursorPos($point.X,$point.Y) | Out-Null
  [RdpInspect]::mouse_event(2,0,0,0,[UIntPtr]::Zero);[RdpInspect]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 300
 }
 $root=[RdpInspect]::GetAncestor([RdpInspect]::GetForegroundWindow(),2)
 if($root -ne $owner){
  $rect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect($owner,[ref]$rect) | Out-Null
  $bitmap=[Drawing.Bitmap]::new($rect.Right-$rect.Left,$rect.Bottom-$rect.Top)
  $graphics=[Drawing.Graphics]::FromImage($bitmap);[RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-1),0,0,0,0,0x13) | Out-Null;Start-Sleep -Milliseconds 800
  try{$graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$bitmap.Size)}finally{[RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-2),0,0,0,0,0x13) | Out-Null}
  try{$bitmap.Save(($CapturePath -replace 'input.png$','desktop.png'),[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$bitmap.Dispose()}
  return
 }
 $focusInfo=[RdpInspect+GuiThread]::new();$focusInfo.Size=[Runtime.InteropServices.Marshal]::SizeOf($focusInfo);[RdpInspect]::GetGUIThreadInfo(0,[ref]$focusInfo) | Out-Null;@{focus=[RdpInspect]::Class($focusInfo.Focus);active=[RdpInspect]::Class($focusInfo.Active)} | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+".focus.json")
 # Real Windows-key input: the local shell must never become foreground.
 foreach($start in $inputContract.native_input.start_keys){
  [RdpInspect]::keybd_event($start.virtual,$start.scan,1,[UIntPtr]::Zero);[RdpInspect]::keybd_event($start.virtual,$start.scan,3,[UIntPtr]::Zero);Start-Sleep -Milliseconds 900
  $bitmap=[Drawing.Bitmap]::new($rect.Right-$rect.Left,$rect.Bottom-$rect.Top);$graphics=[Drawing.Graphics]::FromImage($bitmap)
  try{$graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$bitmap.Size);$bitmap.Save(($CapturePath -replace 'input.png$',$start.screenshot),[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$bitmap.Dispose()}
  $startRemote=([RdpInspect]::GetAncestor([RdpInspect]::GetForegroundWindow(),2) -eq $owner)
  @{windowsKeyKeptRemote=$startRemote;side=$start.name} | ConvertTo-Json | Set-Content -LiteralPath ($CapturePath+'.keys.json')
  [RdpInspect]::keybd_event(0x1B,1,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x1B,1,2,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 450
  if(-not $startRemote){throw 'Windows key activated the local shell.'}
 }
 $run=$inputContract.native_input.run_key
 [RdpInspect]::keybd_event(0x5B,0x5B,1,[UIntPtr]::Zero);[RdpInspect]::keybd_event($run.virtual,$run.scan,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event($run.virtual,$run.scan,2,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x5B,0x5B,3,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 900
 if([RdpInspect]::GetAncestor([RdpInspect]::GetForegroundWindow(),2) -ne $owner){throw 'Win+R activated a local window.'}
 [Windows.Forms.SendKeys]::SendWait('^a')
 [Windows.Forms.Clipboard]::SetText('AstraTerm RDP clipboard check')
 Start-Sleep -Milliseconds 500
 [RdpInspect]::keybd_event(0x11,0x1D,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x56,0x2F,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x56,0x2F,2,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x11,0x1D,2,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 350
 [Windows.Forms.SendKeys]::SendWait('123')
 Start-Sleep -Milliseconds 700
 $rect=[RdpInspect+Rect]::new();[RdpInspect]::GetWindowRect($owner,[ref]$rect) | Out-Null
 $bitmap=[Drawing.Bitmap]::new($rect.Right-$rect.Left,$rect.Bottom-$rect.Top)
 $graphics=[Drawing.Graphics]::FromImage($bitmap)
 try{$graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$bitmap.Size);$bitmap.Save($CapturePath,[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$bitmap.Dispose()}
 [RdpInspect]::keybd_event(0x1B,1,0,[UIntPtr]::Zero);[RdpInspect]::keybd_event(0x1B,1,2,[UIntPtr]::Zero); [RdpInspect]::SetWindowPos($owner,[IntPtr]::new(-2),0,0,0,0,0x13) | Out-Null
}
