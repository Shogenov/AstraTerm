// Native child surface only. The sandboxed renderer never loads COM or receives credentials.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class RdpControl : AxHost {
    public RdpControl() : base("8B918B82-7985-4C24-89DF-C33AD2BBFBCD") { }
    public object Client { get { return GetOcx(); } }
    public bool RetainPresentation;
    protected override void OnLostFocus(EventArgs e) {
        // AxHost's default UIDeactivate clears the RDP presenter when focus moves
        // to Chromium (including mouse crossings). Keep the visible object active;
        // WM_KILLFOCUS still reaches the native control and RdpKeyboard gates input.
        if(!RetainPresentation || !Visible || Disposing)base.OnLostFocus(e);
    }
}

sealed class RdpHost : Form {
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int w, int h, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd,uint command);
    [DllImport("user32.dll")] static extern bool RedrawWindow(IntPtr hwnd,IntPtr rect,IntPtr region,uint flags);
    [DllImport("user32.dll")] static extern IntPtr SetCursor(IntPtr cursor);
    [DllImport("user32.dll",EntryPoint="GetWindowLongW")] static extern int GetWindowStyle(IntPtr hwnd,int index);
    [DllImport("user32.dll",EntryPoint="SetWindowLongW")] static extern int SetWindowStyle(IntPtr hwnd,int index,int style);
    delegate bool EnumWindowsProc(IntPtr hwnd,IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr hwnd,EnumWindowsProc callback,IntPtr data);
    [StructLayout(LayoutKind.Sequential)] struct NativeRect {public int Left,Top,Right,Bottom;}
    [StructLayout(LayoutKind.Sequential)] struct NativePoint {public int X,Y;}
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd,out NativeRect rect);
    [DllImport("user32.dll")] static extern int MapWindowPoints(IntPtr from,IntPtr to,ref NativePoint point,uint count);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static readonly object OutputLock = new object();
    static readonly Guid Events = new Guid("336D5562-EFA8-482E-8CB3-C5C0FC7A7DB6");
    readonly RdpControl control = new RdpControl();
    RdpKeyboard keyboard;
    IntPtr owner;
    bool closing, connected;
    int displayWidth, displayHeight, displayChanges;
    readonly System.Windows.Forms.Timer displayTimer = new System.Windows.Forms.Timer {Interval=300};
    readonly List<Delegate> handlers = new List<Delegate>();
    public static void Emit(object value) { lock(OutputLock) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); } }
    void Bind(int id, Delegate handler) { handlers.Add(handler); ComEventsHelper.Combine(control.Client, Events, id, handler); }
    public RdpHost(IntPtr parent) {
        owner = parent; Text="AstraTerm RDP"; FormBorderStyle=FormBorderStyle.None;
        ShowInTaskbar=false; TopLevel=false; BackColor=Color.FromArgb(16,19,22);
        Controls.Add(control); control.Dock=DockStyle.Fill;
        // Create and parent before mapping: no independent desktop window, even during startup.
        IntPtr handle=Handle;
        if(!IsWindow(parent)) throw new InvalidOperationException();
        SetParent(handle,parent);
        if(GetParent(handle)!=parent) throw new InvalidOperationException();
        ClipNativeSurface();
        IntPtr controlHandle=control.Handle;
        Bind(1,new Action(()=>Emit(new { type="connecting" })));
        Bind(2,new Action(()=>Emit(new { type="connected" })));
        Bind(3,new Action(()=>{
            connected=true;
            control.RetainPresentation=true;
            if(keyboard!=null){keyboard.Dispose();keyboard=null;}
            try {keyboard=new RdpKeyboard(control,()=>owner,()=>connected && !closing && Visible);}
            catch(Exception error) {Emit(new {type="input-error",code=error.HResult});}
            BeginInvoke(new Action(()=>{
                if(!closing && connected){try {Present();}catch(COMException) { /* Retry on the next visible bounds/refresh. */ }}
            }));
            ScheduleDisplay();Emit(new { type="running" });
        }));
        Bind(4,new Action<int>(reason=>{connected=false;control.RetainPresentation=false;if(keyboard!=null){keyboard.Dispose();keyboard=null;}Emit(new {type="disconnected",code=reason});}));
        Bind(10,new Action<int>(code=>Emit(new {type="error",code=code})));
        Bind(18,new Action(()=>Emit(new {type="certificate-warning"})));
        Bind(19,new Action(()=>Emit(new {type="certificate-dismissed"})));
        control.Enter+=(s,e)=>Emit(new {type="focus"});
        displayTimer.Tick+=(s,e)=>{
            displayTimer.Stop();
            if(!connected || !Visible || ClientSize.Width<200 || ClientSize.Height<200)return;
            int width=ClientSize.Width,height=ClientSize.Height;
            if(width==displayWidth && height==displayHeight)return;
            try {
                dynamic rdp=control.Client;
                rdp.UpdateSessionDisplaySettings((uint)width,(uint)height,(uint)width,(uint)height,0u,100u,100u);
                displayWidth=width;displayHeight=height;displayChanges++;
                Emit(new {type="display",width=width,height=height,changes=displayChanges});
            }catch(COMException) { }
        };
        var timer=new System.Windows.Forms.Timer {Interval=1000};
        timer.Tick+=(s,e)=>{if(!IsWindow(owner))Close();};timer.Start();
        Emit(new {type="ready",hwnd=Handle.ToInt64().ToString(),parent=GetParent(Handle).ToInt64().ToString()});
    }
    void ScheduleDisplay() {displayTimer.Stop();displayTimer.Start();}
    void ClipNativeSurface() {
        // The browser paints on hover as well as on focus. Exclude native children
        // from its paint region, and exclude the RDP surface from overlapping
        // Chromium siblings. Z-order alone does not provide either guarantee.
        SetWindowStyle(owner,-16,GetWindowStyle(owner,-16)|0x02000000);
        SetWindowStyle(Handle,-16,GetWindowStyle(Handle,-16)|0x06000000);
        EnumChildWindows(owner,(hwnd,data)=>{
            if(GetParent(hwnd)==owner)SetWindowStyle(hwnd,-16,GetWindowStyle(hwnd,-16)|0x04000000);
            return true;
        },IntPtr.Zero);
    }
    void Present() {
        if(!connected || !Visible)return;
        // OLE in-place activation keeps the live surface presented without taking
        // keyboard focus. AxHost otherwise UI-deactivates on the external host's blur.
        control.DoVerb(-5);
        RedrawWindow(Handle,IntPtr.Zero,IntPtr.Zero,0x0181);
    }
    protected override void WndProc(ref Message message) {
        if(message.Msg==0x0020) { // WM_SETCURSOR: never inherit Chromium's CSS hand cursor.
            if(message.WParam==Handle){SetCursor(Cursors.Default.Handle);message.Result=new IntPtr(1);}
            else message.Result=IntPtr.Zero; // Let the RDP child apply the remote cursor.
            return;
        }
        if(message.Msg==0x0021) { // WM_MOUSEACTIVATE must not activate Chromium's render child.
            message.Result=new IntPtr(1);return; // MA_ACTIVATE
        }
        base.WndProc(ref message);
    }
    public void Command(Dictionary<string,object> args) {
        if(closing)return;
        string op=Convert.ToString(args["op"]);
        dynamic rdp=control.Client;
        if(op=="connect") {
            rdp.Server=Convert.ToString(args["host"]);rdp.UserName=Convert.ToString(args["username"]);
            rdp.Domain=Convert.ToString(args["domain"]);
            rdp.DesktopWidth=Convert.ToInt32(args["width"]);rdp.DesktopHeight=Convert.ToInt32(args["height"]);
            displayWidth=Convert.ToInt32(args["width"]);displayHeight=Convert.ToInt32(args["height"]);
            rdp.ColorDepth=32;
            dynamic advanced=rdp.AdvancedSettings7;
            advanced.RDPPort=Convert.ToInt32(args["port"]);
            advanced.EnableCredSspSupport=true;
            advanced.ContainerHandledFullScreen=1;
            // Native certificate warning requires a human decision; never disable authentication.
            advanced.AuthenticationLevel=2;
            advanced.SmartSizing=true;advanced.RedirectClipboard=true;
            advanced.EnableWindowsKey=1;advanced.AcceleratorPassthrough=1;
            // The visible control lives in a helper, while Electron owns the foreground
            // window. Keep its native input/presentation path active across that boundary.
            // RdpKeyboard separately gates Win gestures on the real foreground/focus HWND.
            advanced.allowBackgroundInput=1;
            advanced.RedirectDrives=false;advanced.RedirectPrinters=false;
            advanced.ClearTextPassword=Convert.ToString(args["password"]);
            rdp.SecuredSettings2.KeyboardHookMode=1;
            rdp.Connect();
            args["password"]=null;
        } else if(op=="reparent") {
            var next=new IntPtr(long.Parse(Convert.ToString(args["parent"])));
            if(!IsWindow(next))throw new InvalidOperationException();
            if(keyboard!=null)keyboard.Release();Hide();SetParent(Handle,next);
            if(GetParent(Handle)!=next)throw new InvalidOperationException();
            owner=next;
            ClipNativeSurface();
            Emit(new {type="parent",parent=owner.ToInt64().ToString()});
        } else if(op=="refresh") {
            // Repaint the retained desktop; focus changes must not renegotiate its resolution.
            if(connected && Visible)RedrawWindow(Handle,IntPtr.Zero,IntPtr.Zero,0x0181);
        } else if(op=="bounds") {
            bool visible=Convert.ToBoolean(args["visible"]);
            if(!visible){if(keyboard!=null)keyboard.Release();Hide();Emit(new {type="surface",visible=false,frontmost=false});return;}
            int w=Math.Max(1,Convert.ToInt32(args["width"])),h=Math.Max(1,Convert.ToInt32(args["height"]));
            // HWND_TOP is required: the Chromium render HWND otherwise covers this native child.
            int x=Convert.ToInt32(args["x"]),y=Convert.ToInt32(args["y"]);
            // Keep this even for identical bounds: ActiveX's cross-process parent needs
            // WM_WINDOWPOSCHANGED to present again when Chromium reactivates its HWND.
            SetWindowPos(Handle,IntPtr.Zero,x,y,w,h,0x0010);
            bool showing=!Visible;
            if(showing){Show();Present();}
            NativeRect actual;GetWindowRect(Handle,out actual);var origin=new NativePoint {X=actual.Left,Y=actual.Top};MapWindowPoints(IntPtr.Zero,owner,ref origin,1);
            Emit(new {type="surface",visible=IsWindowVisible(Handle),frontmost=GetWindow(Handle,3)==IntPtr.Zero,x=origin.X,y=origin.Y,width=actual.Right-actual.Left,height=actual.Bottom-actual.Top});
            if(w!=displayWidth || h!=displayHeight)ScheduleDisplay();
        } else if(op=="focus") {if(Visible && !control.ContainsFocus){control.DoVerb(-4);control.Focus();}}
        else if(op=="close") {Close();}
    }
    protected override void OnFormClosing(FormClosingEventArgs e) {
        closing=true;
        control.RetainPresentation=false;
        if(keyboard!=null){keyboard.Dispose();keyboard=null;}
        displayTimer.Stop();displayTimer.Dispose();
        try {dynamic rdp=control.Client;if(rdp.Connected!=0)rdp.Disconnect();}catch { }
        base.OnFormClosing(e);
    }
    protected override void OnFormClosed(FormClosedEventArgs e) {base.OnFormClosed(e);Application.ExitThread();}
    [STAThread] static int Main(string[] args) {
        try {
            Console.InputEncoding=System.Text.Encoding.UTF8;Console.OutputEncoding=new System.Text.UTF8Encoding(false);
            SetProcessDpiAwarenessContext(new IntPtr(-4));
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            var form=new RdpHost(new IntPtr(long.Parse(args[0])));
            var input=new Thread(()=>{
                try {string line;while((line=Console.ReadLine())!=null) {
                    if(line.Length>131072)break;
                    var command=Json.Deserialize<Dictionary<string,object>>(line);
                    form.BeginInvoke(new Action(()=>{
                        try {form.Command(command);}catch(Exception ex){Emit(new {type="error",code=ex.HResult});}
                        finally {command.Clear();}
                    }));
                }}catch { }
                try {form.BeginInvoke(new Action(()=>form.Close()));}catch { }
            });input.IsBackground=true;input.Start();
            Application.Run(); // The form is a native child, never a top-level application window.
            form.Dispose();return 0;
        }catch(Exception ex){Emit(new {type="error",code=ex.HResult});return 1;}
    }
}
