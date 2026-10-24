using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using Microsoft.Win32;

namespace Averra;
public class Desktop : Application {
 [STAThread] public static void Main(){new Desktop().Run(new MainWindow());}
}
public class MainWindow : Window {
 const string Origin="https://averra-ai.haomingzhang2002.chatgpt.site";
 readonly bool zh=CultureInfo.CurrentUICulture.TwoLetterISOLanguageName=="zh";
 readonly string mode,resources,workspace,name;
 readonly SolidColorBrush accent,ink=new(Color.FromRgb(56,44,51));
 readonly StackPanel content=new(); readonly Grid root=new();
 readonly TextBlock status=new(), output=new(){TextWrapping=TextWrapping.Wrap,FontFamily=new("Consolas"),FontSize=12};
 string section="overview",source="",mcp=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),".codex"),report="";
 bool ready,busy,checking; Process? child; PasswordBox? key,token; TextBox? chat; Slider? weight;
 double qqq=50;string symbol="QQQ";
 readonly List<Button> gated=new();
 string L(string cn,string en)=>zh?cn:en;
 string ModeName=>mode=="agent"?"Agent":mode=="monitor"?"Monitor":"Stock";
 string Prefix=>mode=="agent"?"AVR-A-":mode=="monitor"?"AVR-M-":"AVR-S-";
 public MainWindow(){
  resources=System.IO.Path.Combine(AppContext.BaseDirectory,"Resources");mode=File.ReadAllText(System.IO.Path.Combine(resources,"mode.txt")).Trim();
  if(!new[]{"agent","monitor","stock"}.Contains(mode))throw new InvalidDataException("Unknown app mode");
  name=mode=="stock"?"Averra Stock Agent":mode=="agent"?"Averra Agent":"Averra Monitor";
  Icon=new BitmapImage(new Uri(System.IO.Path.Combine(resources,"AppIcon.ico")));
  workspace=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Averra Desktop",mode,"20261010");Directory.CreateDirectory(workspace);
  LoadPreferences();
  accent=new(mode=="stock"?Color.FromRgb(125,110,209):Color.FromRgb(222,145,184));
  Title=name;Width=1080;Height=800;MinWidth=980;MinHeight=740;Background=new SolidColorBrush(Color.FromRgb(250,247,248));Foreground=ink;
  root.ColumnDefinitions.Add(new(){Width=new GridLength(230)});root.ColumnDefinitions.Add(new());Content=root;
  var sidebar=new StackPanel{Margin=new Thickness(24)};var side=new Border{Background=new SolidColorBrush(mode=="stock"?Color.FromRgb(237,235,250):Color.FromRgb(247,232,240)),Child=sidebar};root.Children.Add(side);
  var brand=new StackPanel{Orientation=Orientation.Horizontal,Margin=new Thickness(0,0,0,12)};
  brand.Children.Add(new Image{Source=new BitmapImage(new Uri(System.IO.Path.Combine(resources,"mark.png"))),Width=28,Height=28,Margin=new Thickness(0,0,10,0)});
  brand.Children.Add(T("Averra",28));sidebar.Children.Add(brand);sidebar.Children.Add(T("",20));
  foreach(var (id,label) in new[]{("overview",L("总览","Overview")),("settings",L("连接与设置","Settings")),("results",L("运行结果","Results"))})sidebar.Children.Add(B(label,()=>{section=id;Draw();}));
  sidebar.Children.Add(T(mode.ToUpperInvariant()+"  ·  V20261010",12));sidebar.Children.Add(T(mode=="agent"?L("只读验证模式","Read-only validation"):L("纯监控模式","Monitor only"),14));
  sidebar.Children.Add(B(L("使用说明","User Guide"),()=>Open(Origin+"/agent/guides/README_WINDOWS_"+(zh?"ZH":"EN")+".html")));
  sidebar.Children.Add(B(L("打开工作区","Open Workspace"),()=>Open(workspace)));
  var scroll=new ScrollViewer{Content=content,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,Margin=new Thickness(32)};Grid.SetColumn(scroll,1);root.Children.Add(scroll);
  Loaded+=async(_,_)=>{Draw();var checks=StartupChecks();await Opening();await checks;await Action("status");};
  Closed+=(_,_)=>{try{child?.Kill(true);}catch{}};
 }
 TextBlock T(string text,double size=14)=>new(){Text=text,FontSize=size,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,0,0,12),Foreground=ink};
 Button B(string label,Action click,bool primary=false,bool gate=false){var b=new Button{Content=label,Padding=new Thickness(16,10,16,10),Margin=new Thickness(0,0,8,10),Background=primary?ink:Brushes.White,Foreground=primary?Brushes.White:ink,BorderBrush=new SolidColorBrush(Color.FromRgb(225,217,221)),BorderThickness=new Thickness(1),Cursor=System.Windows.Input.Cursors.Hand};b.Click+=(_,_)=>click();if(gate){gated.Add(b);b.IsEnabled=ready&&!busy;}return b;}
 void Card(params UIElement[] elements){var stack=new StackPanel();foreach(var e in elements)stack.Children.Add(e);content.Children.Add(new Border{Child=stack,Padding=new Thickness(22),Background=Brushes.White,CornerRadius=new CornerRadius(16),Margin=new Thickness(0,0,0,20)});}
 void Draw(){
  content.Children.Clear();gated.Clear();content.Children.Add(T(section=="settings"?L("连接与设置","Connections & Settings"):section=="results"?L("运行结果","Results"):name,32));
  content.Children.Add(T(mode=="agent"?L("先连接，再安心检查","Connect first, then check with confidence"):L("关注市场，保留你的判断","Follow the market. Keep your own judgment."),15));
  if(!ready){key=new PasswordBox{Margin=new Thickness(0,0,0,12),Padding=new Thickness(10)};Card(T(L("启用你的 ","Enable your ")+ModeName,20),T(L("输入 ","Enter ")+Prefix+L(" 密钥"," key")),key,B(L("验证密钥","Verify Key"),async()=>await Verify(),true),T(L("密钥仅发送给 Averra，不保存明文","Your key goes only to Averra; plaintext is not saved."),12));}
  if(section=="overview"){
   var runlabel=mode=="stock"?L("读取监控状态","Read Monitor Status"):mode=="agent"?L("运行只读验证","Run Read-only Check"):L("运行一次监控","Run Monitor Once");
   Card(T(mode=="stock"?L("看清信号，再做决定","Understand the signal. Make your own decision."):mode=="agent"?L("你的账户检查助手","Your account check assistant"):L("QQQ 与 SMH，一处查看","QQQ and SMH in one place"),22),T(mode=="stock"?L("仅读取已有监控记录，不启动付费 AI、全市场扫描或交易","Reads saved monitoring records; no paid AI, market-wide scan or trades."):L("按需检查，不自动运行、不安装后台任务，没有实盘启动入口","Checks on demand. No automatic run, background installation, or live-trading launch.")),B(runlabel,async()=>await Action("run"),true,true),B(L("离线示例","Offline Example"),async()=>await Action("demo")));
   Card(T(L("配置与连接","Configuration & Connections"),20),T(mode=="stock"?(source==""?L("尚未选择监控目录","No monitor folder selected"):source):(mode=="agent"?"SMH 70% / QQQ 20% / SGOV 10%":$"QQQ {qqq:0}% / SMH {100-qqq:0}%")),B(L("连接与设置","Connections & Settings"),()=>{section="settings";Draw();}));
  }
  if(section=="settings"){
   Card(T(L("启动检测","Startup Checks"),20),T(report),B(L("重新检测","Check Again"),async()=>{await StartupChecks();Draw();}));
   if(mode=="agent")Card(T("MCP",20),T(mcp),B(L("选择 MCP 配置目录","Choose MCP Folder"),()=>{var p=Folder();if(p!=null){mcp=p;SavePreferences();Draw();}}),B(L("连接 MCP","Connect MCP"),async()=>await Action("mcp_connect"),true,true),T(L("只检查握手和只读工具；需要 Windows 可运行的服务命令和有效登录","Handshake/read-only tool check. Requires Windows-compatible server commands and a valid login."),12));
   token=new PasswordBox{Padding=new Thickness(10),Margin=new Thickness(0,0,0,10)};chat=new TextBox{Padding=new Thickness(10),Margin=new Thickness(0,0,0,10)};
   Card(T("Telegram",20),T(L("Bot Token（留空可使用已保存配置）","Bot Token (leave blank to use saved settings)")),token,T("Chat ID"),chat,B(L("导入已有配置","Import Existing Settings"),ImportTelegram),B(L("连接 Telegram","Connect Telegram"),async()=>await Telegram(),true,true),T(L("仅检查机器人和会话，使用 Windows DPAPI 加密保存；不发送消息","Checks bot/chat only; stores with Windows DPAPI encryption. Sends no messages."),12));
   if(mode=="stock")Card(T(L("本地监控工程","Local Monitor Project"),20),T(source==""?L("选择已有 Stock 工程，不执行其中代码或读取 .env","Choose an existing Stock project; its code and .env are not read."):source),B(L("选择监控目录","Choose Monitor Folder"),()=>{var p=Folder();if(p!=null){source=p;SavePreferences();Draw();}}),B(L("读取监控状态","Read Monitor Status"),async()=>await Action("run"),true,true));
   else if(mode=="agent"){Card(T("SMH 70% / QQQ 20% / SGOV 10%",20),T(L("回调 SGOV 最多 20%；复苏最多 5%。公开 App 保持只读。","Pullback SGOV up to 20%; recovery at most 5%. Public app stays read-only.")),B(L("检查配置","Check Configuration"),async()=>await Action("check"),false,true));}
   else{
    weight=new Slider{Minimum=0,Maximum=100,Value=qqq,TickFrequency=1,IsSnapToTickEnabled=true,Margin=new Thickness(0,5,0,15)};weight.ValueChanged+=(_,_)=>{qqq=weight.Value;SavePreferences();};
    var pick=new ComboBox{ItemsSource=new[]{"QQQ","SMH"},SelectedItem=symbol,Margin=new Thickness(0,0,0,10)};pick.SelectionChanged+=(_,_)=>{symbol=(string)pick.SelectedItem;SavePreferences();};
    Card(T(L("QQQ 分配比例 %（SMH 为剩余比例）","QQQ allocation % (SMH is the remainder)"),20),weight,mode=="monitor"?pick:T("QQQ / SMH"),B(L("保存比例","Save Allocation"),async()=>await Action("weights"),true,true),B(L("检查配置","Check Configuration"),async()=>await Action("check"),false,true));
   }
  }
  if(section=="results")Card(T(L("结果与诊断","Results & Diagnostics"),20),T(output.Text==""?L("尚未运行","Nothing has run yet"):output.Text,12),B(L("复制","Copy"),()=>Clipboard.SetText(output.Text)));
  content.Children.Add(status);status.Text=ready?L("已验证 · 不会自动开始运行","Verified · Will not start automatically"):L("待验证","Verify first");content.Children.Add(T(L("独立工作区 · 无后台任务 · Windows 64 位","Separate workspace · No background jobs · Windows x64"),11));
 }
 void SavePreferences(){try{File.WriteAllText(System.IO.Path.Combine(workspace,"preferences.json"),JsonSerializer.Serialize(new{mcp,source,qqq,symbol}));}catch{status.Text=L("偏好未保存","Preferences could not be saved");}}
 void LoadPreferences(){try{using var d=JsonDocument.Parse(File.ReadAllText(System.IO.Path.Combine(workspace,"preferences.json")));var r=d.RootElement;mcp=r.GetProperty("mcp").GetString()??mcp;source=r.GetProperty("source").GetString()??"";qqq=Math.Clamp(r.GetProperty("qqq").GetDouble(),0,100);symbol=r.GetProperty("symbol").GetString()=="SMH"?"SMH":"QQQ";}catch{}}
 static string? Folder(){var d=new OpenFolderDialog{Multiselect=false};return d.ShowDialog()==true?d.FolderName:null;}
 static void Open(string path)=>Process.Start(new ProcessStartInfo(path){UseShellExecute=true});
 HttpClient Client(int seconds=20)=>new(new HttpClientHandler{AllowAutoRedirect=false}){Timeout=TimeSpan.FromSeconds(seconds)};
 async Task Verify(){if(busy)return;var value=key?.Password.Trim()??"";if(!Regex.IsMatch(value,"^"+Prefix+"[0-9a-f]{64}$")){status.Text=L("密钥格式不正确","Invalid key format");return;}busy=true;try{using var http=Client();using var response=await http.PostAsync(Origin+"/api/setup-key/verify",new StringContent(JsonSerializer.Serialize(new{mode,key=value}),Encoding.UTF8,"application/json"));var result=JsonDocument.Parse(await response.Content.ReadAsStringAsync());if(!response.IsSuccessStatusCode||!result.RootElement.GetProperty("ok").GetBoolean())throw new InvalidOperationException();SecureStore.Write(System.IO.Path.Combine(workspace,"activation.dpapi"),JsonSerializer.Serialize(new{mode,verifiedAt=DateTimeOffset.UtcNow.ToString("O")}));ready=true;Draw();status.Text=L("验证成功，不会自动运行","Verified. No automatic run.");}catch{status.Text=L("未通过验证，请检查密钥与网络","Not verified. Check your key and network.");}finally{if(key!=null)key.Password="";busy=false;foreach(var b in gated)b.IsEnabled=ready;}}
 void Receipt(){var p=System.IO.Path.Combine(workspace,".desktop-verified.json");if(!ready){if(File.Exists(p))File.Delete(p);return;}File.WriteAllText(p,JsonSerializer.Serialize(new{mode,verifiedAt=DateTimeOffset.UtcNow.ToString("O")}));}
 async Task Action(string action){if(busy)return;if(action=="status"){try{var doc=JsonDocument.Parse(SecureStore.Read(System.IO.Path.Combine(workspace,"activation.dpapi")));ready=doc.RootElement.GetProperty("mode").GetString()==mode;}catch{ready=false;}}
  if(!new[]{"status","demo"}.Contains(action)&&!ready){status.Text=L("请先验证密钥","Verify your key first");return;}busy=true;foreach(var b in gated)b.IsEnabled=false;
  try{
   var data=JsonSerializer.Serialize(new{action,symbol,qqq=qqq/100,language=zh?"zh":"en",source});
   // Initialize using existing adapters before writing a mode receipt.
   if(action!="status")Receipt();
   var psi=new ProcessStartInfo(System.IO.Path.Combine(resources,"python","python.exe")){UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true};
   foreach(var a in new[]{System.IO.Path.Combine(resources,mode=="stock"?"stock_bridge.py":"bridge.py"),resources,workspace,mode})psi.ArgumentList.Add(a);
   psi.Environment.Clear();foreach(var k in new[]{"SystemRoot","WINDIR","TEMP","TMP","USERPROFILE","APPDATA","LOCALAPPDATA","PATH"}){var v=Environment.GetEnvironmentVariable(k);if(v!=null)psi.Environment[k]=v;}
   psi.Environment["PYTHONNOUSERSITE"]="1";psi.Environment["PYTHONDONTWRITEBYTECODE"]="1";psi.Environment["PYTHONIOENCODING"]="utf-8";psi.Environment["PYTHONUNBUFFERED"]="1";psi.Environment["SSL_CERT_FILE"]=System.IO.Path.Combine(resources,"cert.pem");if(mode=="agent")psi.Environment["CODEX_HOME"]=mcp;
   child=Process.Start(psi)??throw new IOException();var stdout=child.StandardOutput.ReadToEndAsync();var stderr=child.StandardError.ReadToEndAsync();await child.StandardInput.WriteAsync(data);child.StandardInput.Close();using var cancel=new CancellationTokenSource(TimeSpan.FromSeconds(190));try{await child.WaitForExitAsync(cancel.Token);}catch{child.Kill(true);throw;}
   var parsed=JsonDocument.Parse(await stdout);await stderr;var r=parsed.RootElement;
   if(r.TryGetProperty("error",out var error))output.Text=error.GetString()??"";else if(action!="status")output.Text=r.TryGetProperty("output",out var o)?o.GetString()??"":L("完成","Done");
   if(new[]{"demo","check","run"}.Contains(action))section="results";Draw();
  }catch{output.Text=L("检查未完成，请查看 README 中的 Windows 排错步骤；未启动交易","Check did not complete. See Windows troubleshooting in README. No trades started.");section="results";Draw();}finally{child=null;busy=false;foreach(var b in gated)b.IsEnabled=ready;}
 }
 void ImportTelegram(){var d=new OpenFileDialog{Filter="Configuration|*.*"};if(d.ShowDialog()!=true)return;try{var values=new Dictionary<string,string>();foreach(var line in File.ReadLines(d.FileName)){var i=line.IndexOf('=');if(i>0&&!line.TrimStart().StartsWith('#'))values[line[..i].Trim()]=line[(i+1)..].Trim().Trim('\"','\'');}var prefix=mode=="stock"?"STOCK_":"";token!.Password=values.GetValueOrDefault(prefix+"TELEGRAM_BOT_TOKEN",mode=="stock"?values.GetValueOrDefault("STOCK_TELEGRAM_TOKEN",""):"");chat!.Text=values.GetValueOrDefault(prefix+"TELEGRAM_CHAT_ID","");status.Text=L("已在本机读取，请连接后检查","Read locally. Connect to validate.");}catch{status.Text=L("配置读取失败","Could not read settings");}}
 async Task Telegram(){if(!ready||busy)return;busy=true;try{var file=System.IO.Path.Combine(workspace,"telegram.dpapi");var credentials=token!.Password==""&&chat!.Text==""?JsonSerializer.Deserialize<Dictionary<string,string>>(SecureStore.Read(file))!:new Dictionary<string,string>{{"token",token!.Password.Trim()},{"chatID",chat!.Text.Trim()}};var t=credentials["token"];var id=credentials["chatID"];if(!Regex.IsMatch(t,"^[0-9]+:[A-Za-z0-9_-]{20,}$")||!Regex.IsMatch(id,"^-?[0-9]+$"))throw new ArgumentException();using var http=Client(15);string username="";foreach(var method in new[]{"getMe","getChat"}){using var response=await http.PostAsync("https://api.telegram.org/bot"+t+"/"+method,new StringContent(method=="getChat"?JsonSerializer.Serialize(new{chat_id=id}):"{}",Encoding.UTF8,"application/json"));var doc=JsonDocument.Parse(await response.Content.ReadAsStringAsync());if(!response.IsSuccessStatusCode||!doc.RootElement.GetProperty("ok").GetBoolean())throw new InvalidOperationException();if(method=="getMe"){var bot=doc.RootElement.GetProperty("result");if(!bot.GetProperty("is_bot").GetBoolean())throw new InvalidOperationException();username=bot.GetProperty("username").GetString()??"";}}SecureStore.Write(file,JsonSerializer.Serialize(credentials));token!.Password="";chat!.Text="";status.Text=L("机器人与会话验证通过，已加密保存","Bot and chat verified. Saved with encryption.")+" @"+username;}catch{status.Text=L("Telegram 未连接；已有配置不变，请检查 README","Telegram not connected. Saved settings unchanged; see README.");}finally{busy=false;}}
 async Task StartupChecks(){if(checking)return;checking=true;var lines=new List<string>();try{
  lines.Add(L("CPU 核心：","CPU cores: ")+Environment.ProcessorCount);var cpu=await Task.Run(()=>{var sw=Stopwatch.StartNew();ulong x=0x12345678;for(var i=0;i<2000000;i++){x^=x<<13;x^=x>>7;x^=x<<17;}return $"{sw.Elapsed.TotalMilliseconds:0.0} ms · {x}";});lines.Add(L("200 万次整数运算：","2 million integer operations: ")+cpu);
  var memory=await Task.Run(()=>{var bytes=new byte[32*1024*1024];for(int i=0;i<bytes.Length;i+=4096)bytes[i]=(byte)((i/4096)%251);for(int i=0;i<bytes.Length;i+=4096)if(bytes[i]!=(byte)((i/4096)%251))throw new IOException();var m=new Native.Memory{Length=(uint)Marshal.SizeOf<Native.Memory>()};if(!Native.GlobalMemoryStatusEx(ref m))throw new IOException();return $"{m.Total/1073741824.0:0.0} GB · 32 MiB";});lines.Add(L("物理内存与分页读写通过：","Physical RAM and page read/write passed: ")+memory);
  double max=0;for(int i=0;i<12;i++){var sw=Stopwatch.StartNew();await Task.Delay(25);max=Math.Max(max,Math.Max(0,sw.Elapsed.TotalMilliseconds-25));}lines.Add(L("短时响应额外延迟：","Short response extra delay: ")+$"{max:0.0} ms");lines.Add(L("CPU 温度：Windows 无通用接口，未检测（不是通过）","CPU thermal state: no universal Windows API; not tested, not a pass"));
  try{using var http=Client(6);var sw=Stopwatch.StartNew();using var response=await http.SendAsync(new HttpRequestMessage(HttpMethod.Head,Origin));lines.Add(response.IsSuccessStatusCode?L("Averra HTTPS 可达：","Averra HTTPS reachable: ")+$"{sw.Elapsed.TotalMilliseconds:0} ms":L("网络响应受限：","Network response limited: ")+(int)response.StatusCode);}catch{lines.Add(L("网络检查未完成，可离线进入","Network check incomplete; offline entry available"));}
 }catch{lines.Add(L("部分检测未完成，未伪报通过","Some checks incomplete; no false pass"));}finally{report=string.Join("\n",lines)+"\n\n"+L("轻量检查，不是硬件诊断、长期稳定性测试或交易就绪认证；不上传结果","Lightweight checks, not hardware diagnostics, stability certification or trading readiness. Results are not uploaded.");checking=false;}}
 async Task Opening(){if(!SystemParameters.ClientAreaAnimation)return;var overlay=new Border{Background=Background};Grid.SetColumnSpan(overlay,2);root.Children.Add(overlay);var canvas=new Canvas();overlay.Child=canvas;var center=new Ellipse{Width=24,Height=24,Fill=accent};canvas.Children.Add(center);var cx=Width/2-12;var cy=Height/2-50;Canvas.SetLeft(center,cx);Canvas.SetTop(center,cy);var nodes=new List<StackPanel>();foreach(var (label,x,y) in new[]{("CPU",-130d,-80d),(L("内存","Memory"),130d,-80d),(L("响应","Response"),-130d,80d),(L("网络","Network"),130d,80d)}){var node=new StackPanel{Width=110,Opacity=0};node.Children.Add(new Ellipse{Width=12,Height=12,Fill=accent,HorizontalAlignment=HorizontalAlignment.Center,Margin=new Thickness(0,0,0,10)});node.Children.Add(new TextBlock{Text=label,TextAlignment=TextAlignment.Center,Foreground=ink});Canvas.SetLeft(node,cx+x-43);Canvas.SetTop(node,cy+y);canvas.Children.Add(node);nodes.Add(node);node.BeginAnimation(OpacityProperty,new DoubleAnimation(0,1,TimeSpan.FromSeconds(.55)){BeginTime=TimeSpan.FromSeconds(.4+nodes.Count*.12),EasingFunction=new CubicEase{EasingMode=EasingMode.EaseInOut}});}
  var note=T(L("检测设备，准备工作区","Checking your device. Preparing your workspace."),16);Canvas.SetLeft(note,cx-130);Canvas.SetTop(note,cy+165);canvas.Children.Add(note);
  var skip=B(L("跳过","Skip"),()=>{root.Children.Remove(overlay);});Canvas.SetLeft(skip,cx-30);Canvas.SetTop(skip,cy+215);canvas.Children.Add(skip);
  await Task.Delay(3450);foreach(var n in nodes){n.BeginAnimation(OpacityProperty,new DoubleAnimation(1,0,TimeSpan.FromSeconds(.7)));n.BeginAnimation(Canvas.LeftProperty,new DoubleAnimation(Canvas.GetLeft(n),cx-43,TimeSpan.FromSeconds(.9)){EasingFunction=new CubicEase{EasingMode=EasingMode.EaseInOut}});n.BeginAnimation(Canvas.TopProperty,new DoubleAnimation(Canvas.GetTop(n),cy,TimeSpan.FromSeconds(.9)){EasingFunction=new CubicEase{EasingMode=EasingMode.EaseInOut}});}await Task.Delay(900);
  center.BeginAnimation(Canvas.LeftProperty,new DoubleAnimation(cx,28,TimeSpan.FromSeconds(.7)){EasingFunction=new CubicEase{EasingMode=EasingMode.EaseInOut}});center.BeginAnimation(Canvas.TopProperty,new DoubleAnimation(cy,28,TimeSpan.FromSeconds(.7)){EasingFunction=new CubicEase{EasingMode=EasingMode.EaseInOut}});note.BeginAnimation(OpacityProperty,new DoubleAnimation(1,0,TimeSpan.FromSeconds(.6)));await Task.Delay(700);root.Children.Remove(overlay);
 }
}
public static class Native {
 [StructLayout(LayoutKind.Sequential)]public struct Memory{public uint Length,Load;public ulong Total,Available,PageTotal,PageAvailable,VirtualTotal,VirtualAvailable,Extended;}
 [DllImport("kernel32.dll",SetLastError=true)]public static extern bool GlobalMemoryStatusEx(ref Memory memory);
}
public static class SecureStore {
 [StructLayout(LayoutKind.Sequential)]struct Blob{public int Size;public IntPtr Data;}
 [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]static extern bool CryptProtectData(ref Blob input,string? description,IntPtr entropy,IntPtr reserved,IntPtr prompt,uint flags,out Blob output);
 [DllImport("crypt32.dll",SetLastError=true)]static extern bool CryptUnprotectData(ref Blob input,IntPtr description,IntPtr entropy,IntPtr reserved,IntPtr prompt,uint flags,out Blob output);
 [DllImport("kernel32.dll")]static extern IntPtr LocalFree(IntPtr memory);
 static byte[] Transform(byte[] bytes,bool encrypt){var input=new Blob{Size=bytes.Length,Data=Marshal.AllocHGlobal(bytes.Length)};Marshal.Copy(bytes,0,input.Data,bytes.Length);try{Blob output;var ok=encrypt?CryptProtectData(ref input,"Averra",IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,1,out output):CryptUnprotectData(ref input,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,1,out output);if(!ok)throw new IOException("Protected storage unavailable");try{var result=new byte[output.Size];Marshal.Copy(output.Data,result,0,result.Length);return result;}finally{LocalFree(output.Data);}}finally{Marshal.Copy(new byte[bytes.Length],0,input.Data,bytes.Length);Marshal.FreeHGlobal(input.Data);}}
 public static void Write(string file,string text){var bytes=Encoding.UTF8.GetBytes(text);try{var encrypted=Transform(bytes,true);File.WriteAllBytes(file+".tmp",encrypted);File.Move(file+".tmp",file,true);}finally{CryptographicOperations.ZeroMemory(bytes);}}
 public static string Read(string file){var bytes=Transform(File.ReadAllBytes(file),false);try{return Encoding.UTF8.GetString(bytes);}finally{CryptographicOperations.ZeroMemory(bytes);}}
}
