// The ActiveX keyboard hook cannot own the foreground Electron process.
// Capture only Win-key gestures while this native RDP control has real input focus.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Windows.Forms;

sealed class RdpKeyboard : IDisposable {
    delegate IntPtr HookProc(int code, IntPtr message, IntPtr data);
    [StructLayout(LayoutKind.Sequential)] struct Key {public uint Virtual, Scan, Flags, Time;public UIntPtr Extra;}
    [StructLayout(LayoutKind.Sequential)] struct GuiThread {
        public int Size,Flags;public IntPtr Active,Focus,Capture,Menu,Move,Caret;public int Left,Top,Right,Bottom;
    }
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int type,HookProc callback,IntPtr module,uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook,int code,IntPtr message,IntPtr data);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent,IntPtr child);
    [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread,ref GuiThread info);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr window,uint flags);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window,uint message,UIntPtr key,IntPtr data);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("user32.dll")] static extern uint MapVirtualKey(uint key,uint mode);
    readonly Control control;
    readonly Func<IntPtr> owner;
    readonly Func<bool> available;
    readonly HookProc callback;
    readonly Timer timer=new Timer {Interval=50};
    readonly Dictionary<uint,int> down=new Dictionary<uint,int>();
    readonly HashSet<uint> swallowed=new HashSet<uint>();
    IntPtr hook;
    IntPtr inputWindow;
    bool disposed;
    public RdpKeyboard(Control view,Func<IntPtr> parent,Func<bool> active) {
        control=view;owner=parent;available=active;callback=HandleKey;
        hook=SetWindowsHookEx(13,callback,GetModuleHandle(null),0);
        if(hook==IntPtr.Zero)throw new InvalidOperationException("RDP keyboard hook unavailable.");
        timer.Tick+=(s,e)=>{if(!Focused())Release();};timer.Start();
    }
    bool Focused() {
        if(disposed || !available() || !control.Visible)return false;
        if(GetAncestor(GetForegroundWindow(),2)!=owner())return false;
        var info=new GuiThread {Size=Marshal.SizeOf(typeof(GuiThread))};
        bool focused=GetGUIThreadInfo(0,ref info) && (info.Focus==control.Handle || IsChild(control.Handle,info.Focus));
        if(focused)inputWindow=info.Focus;
        return focused;
    }
    IntPtr HandleKey(int code,IntPtr message,IntPtr pointer) {
        if(code<0 || disposed)return CallNextHookEx(hook,code,message,pointer);
        var key=(Key)Marshal.PtrToStructure(pointer,typeof(Key));
        bool up=(key.Flags&0x80)!=0,win=key.Virtual==0x5b || key.Virtual==0x5c;
        bool focused=Focused();
        if(!focused)Release();
        // Matching releases remain swallowed if focus changed while a captured key was down.
        bool consume=swallowed.Contains(key.Virtual) || (focused && (win || down.ContainsKey(0x5b) || down.ContainsKey(0x5c)));
        if(!consume)return CallNextHookEx(hook,code,message,pointer);
        uint scan=key.Scan!=0?key.Scan:MapVirtualKey(key.Virtual,0);
        int data=1 | ((int)scan<<16) | (((key.Flags&1)!=0)?0x01000000:0) | (((key.Flags&0x20)!=0)?0x20000000:0);
        if(up) {
            swallowed.Remove(key.Virtual);
            if(down.Remove(key.Virtual))Send(key.Virtual,true,data);
        }else {
            swallowed.Add(key.Virtual);
            if(focused){bool repeated=down.ContainsKey(key.Virtual);down[key.Virtual]=data;Send(key.Virtual,false,data | (repeated?0x40000000:0));}
        }
        return new IntPtr(1);
    }
    void Send(uint key,bool up,int data) {
        if(inputWindow==IntPtr.Zero || (inputWindow!=control.Handle && !IsChild(control.Handle,inputWindow)))return;
        uint message=(data&0x20000000)!=0?(up?0x105u:0x104u):(up?0x101u:0x100u);
        // Address only the focused input HWND of this ActiveX control. Posting here
        // bypasses the host shell; it does not inject keyboard input into other apps.
        PostMessage(inputWindow,message,new UIntPtr(key),new IntPtr(data | (up?unchecked((int)0xc0000000):0)));
    }
    public void Release() {
        if(down.Count==0)return;
        // Release chord keys before Win, avoiding a stuck modifier after blur/hide/reparent.
        foreach(var key in down)if(key.Key!=0x5b && key.Key!=0x5c)Send(key.Key,true,key.Value);
        foreach(var key in down)if(key.Key==0x5b || key.Key==0x5c)Send(key.Key,true,key.Value);
        down.Clear();
    }
    public void Dispose() {
        if(disposed)return;
        Release();disposed=true;timer.Stop();timer.Dispose();
        if(hook!=IntPtr.Zero){UnhookWindowsHookEx(hook);hook=IntPtr.Zero;}
        swallowed.Clear();
    }
}
