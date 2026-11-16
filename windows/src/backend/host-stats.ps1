# Read-only local host inventory. No remoting, authentication, UI or settings changes.
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$result=@{captured=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0;rx=$null;tx=$null;interfaces=@();users=$null;disks=@()}
try {
 # Select the preferred default route per address family; summing physical and
 # VPN adapters would count the same traffic twice.
 $routes=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0','::/0' -ErrorAction SilentlyContinue | Where-Object {$_.State -eq 'Alive'})
 $adapters=@($routes | Group-Object AddressFamily | ForEach-Object {$_.Group | Sort-Object @{Expression={$_.RouteMetric+$_.InterfaceMetric}} | Select-Object -First 1 -ExpandProperty InterfaceAlias} | Sort-Object -Unique)
 $network=@($adapters | ForEach-Object {Get-NetAdapterStatistics -Name $_})
 if($network.Count){$result.rx=($network | Measure-Object ReceivedBytes -Sum).Sum;$result.tx=($network | Measure-Object SentBytes -Sum).Sum;$result.interfaces=$adapters}
}catch{}
try {
 $mounts=@(Get-CimInstance Win32_MountPoint)
 $result.disks=@(Get-CimInstance Win32_Volume -Filter 'DriveType=3' | Where-Object {$_.Capacity -gt 0} | ForEach-Object {
  $volume=$_
  $paths=@($mounts | Where-Object {$_.Volume.DeviceID -eq $volume.DeviceID} | ForEach-Object {$_.Directory.Name})
  if($volume.DriveLetter){$paths+=($volume.DriveLetter+'\')}
  $used=[double]$_.Capacity-[double]$_.FreeSpace
  $paths | Sort-Object -Unique | ForEach-Object {@{path=$_;used=$used;total=[double]$volume.Capacity;percent=[Math]::Round(100*$used/[double]$volume.Capacity,1)}}
 })
}catch{}
try {
 Add-Type @'
using System;using System.Collections.Generic;using System.Runtime.InteropServices;
public static class HostSessions {
 [StructLayout(LayoutKind.Sequential)] struct Session{public int Id;public IntPtr Station;public int State;}
 [DllImport("wtsapi32.dll",CharSet=CharSet.Unicode)] static extern bool WTSEnumerateSessions(IntPtr server,int reserved,int version,out IntPtr sessions,out int count);
 [DllImport("wtsapi32.dll",CharSet=CharSet.Unicode)] static extern bool WTSQuerySessionInformation(IntPtr server,int session,int info,out IntPtr data,out int bytes);
 [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr data);
 public static Dictionary<string,int> Read(){
  var result=new Dictionary<string,int>();IntPtr data;int count;
  if(!WTSEnumerateSessions(IntPtr.Zero,0,1,out data,out count))throw new InvalidOperationException();
  try{for(int index=0;index<count;index++){
   var item=(Session)Marshal.PtrToStructure(IntPtr.Add(data,index*Marshal.SizeOf(typeof(Session))),typeof(Session));
   if(item.State!=0 && item.State!=1 && item.State!=4)continue;
   IntPtr name;int bytes;if(!WTSQuerySessionInformation(IntPtr.Zero,item.Id,5,out name,out bytes))continue;
   try{var user=Marshal.PtrToStringUni(name);if(!String.IsNullOrEmpty(user)){if(!result.ContainsKey(user))result[user]=0;result[user]++;}}finally{WTSFreeMemory(name);}
  }}finally{WTSFreeMemory(data);}return result;
 }
}
'@
 $counts=[HostSessions]::Read();$result.users=@($counts.Keys | Sort-Object | ForEach-Object {@{name=$_;count=$counts[$_]}})
}catch{}
$result | ConvertTo-Json -Depth 5 -Compress
