import SwiftUI
import AppKit
import Darwin

@main struct AverraApp: App {
    @StateObject private var model = Model()
    var body: some Scene {
        WindowGroup { ContentView(model:model).frame(minWidth:980,minHeight:740) }
        .windowStyle(.hiddenTitleBar)
        .commands { CommandGroup(replacing:.newItem) {} }
    }
}
@MainActor final class Model: ObservableObject {
    let mode=Bundle.main.object(forInfoDictionaryKey:"AverraMode") as? String ?? "monitor"
    var name:String { mode == "stock" ? "Averra Stock Agent" : mode == "agent" ? "Averra Agent" : "Averra Monitor" }
    var keyModeName:String { mode=="stock" ? "Stock" : mode=="agent" ? "Agent" : "Monitor" }
    var keyPrefix:String { mode=="stock" ? "AVR-S-" : mode=="agent" ? "AVR-A-" : "AVR-M-" }
    @Published var stockSource=""
    func chooseStockSource(){
        let panel=NSOpenPanel();panel.canChooseDirectories=true;panel.canChooseFiles=false;panel.allowsMultipleSelection=false;panel.message=L("选择现有 Stock Agent 工程目录。仅读取监控状态，不执行其中代码、不复制密钥、不修改原目录。", "Choose an existing Stock Agent folder. Only monitor state is read; no code is run, credentials copied, or source files changed.")
        if panel.runModal() == .OK,let url=panel.url{stockSource=url.path}
    }
    let resources=Bundle.main.resourceURL!
    var workspace:URL { FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("Averra Desktop/\(mode)/20261008") }
    @Published var ready=false
    @Published var busy=false
    @Published var key=""
    @Published var log=""
    @Published var resultState="empty"
    @Published var resultTime:Date?
    @Published var recordDate=""
    @Published var resultExplanation=""
    @Published var status=L("正在准备独立工作区", "Preparing your separate workspace")
    @Published var qqq=50.0
    @Published var symbol="QQQ"
    @Published var mcpHome=UserDefaults.standard.string(forKey:"mcpHome") ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex").path
    @Published var mcpConnected=false
    func chooseMCP(){
        let panel=NSOpenPanel();panel.canChooseDirectories=true;panel.canChooseFiles=false;panel.allowsMultipleSelection=false;panel.message=L("选择包含 config.toml 的 Codex MCP 配置目录，不会复制凭据", "Choose the Codex MCP folder containing config.toml. Credentials will not be copied.")
        if panel.runModal() == .OK,let url=panel.url{mcpHome=url.path;UserDefaults.standard.set(mcpHome,forKey:"mcpHome");mcpConnected=false}
    }
    @Published var startupVisible=true
    @Published var startupRunning=false
    @Published var startupStep=0
    @Published var startupResults:[String]=[]
    @Published var startupReport=""
    @Published var startupNetworkOK=false
    @Published var telegramToken=""
    @Published var telegramChatID=""
    @Published var telegramSaved=false
    @Published var telegramConnected=false
    @Published var telegramChecking=false
    @Published var telegramStatus=L("配置 Telegram，方便连接你的机器人", "Connect your Telegram bot")
    @Published var telegramUsername=""
    func importTelegram(){
                let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.canChooseFiles=true;panel.allowsMultipleSelection=false
        panel.showsHiddenFiles=true;panel.message=L("选择已有的 Telegram 配置文件；仅在本机读取 Bot Token 和 Chat ID，不修改原文件", "Choose an existing Telegram configuration file. Only Bot Token and Chat ID are read locally; the original file is not changed.")
        if panel.runModal() == .OK,let url=panel.url,let text=try? String(contentsOf:url,encoding:.utf8){
            var values:[String:String]=[:]
            for raw in text.components(separatedBy:.newlines){
                let line=raw.trimmingCharacters(in:.whitespaces)
                if line.hasPrefix("#"){continue}
                let parts=line.components(separatedBy:"=");if parts.count>=2{values[parts[0].trimmingCharacters(in:.whitespaces)]=parts.dropFirst().joined(separator:"=").trimmingCharacters(in:CharacterSet(charactersIn:" \"'"))}
            }
            guard let token=values[mode=="stock" ? "STOCK_TELEGRAM_BOT_TOKEN" : "TELEGRAM_BOT_TOKEN"] ?? (mode=="stock" ? values["STOCK_TELEGRAM_TOKEN"] : nil),let chat=values[mode=="stock" ? "STOCK_TELEGRAM_CHAT_ID" : "TELEGRAM_CHAT_ID"] else{telegramStatus=L("文件中缺少对应模式的 Bot Token / Chat ID，请手动填写", "Bot Token / Chat ID for this mode were not found. Enter them manually.");return}
            telegramToken=token;telegramChatID=chat;telegramConnected=false;telegramStatus=L("已在本机读取，点击连接后检查并保存到钥匙串", "Read locally. Connect to check and save to Keychain.")
        }
    }
    func connectTelegram() async {
        guard ready,!telegramChecking else{return}
        telegramChecking=true;telegramConnected=false;telegramStatus=L("正在检查机器人身份与目标会话", "Checking the bot and target chat")
        defer{telegramChecking=false}
        let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=10;config.timeoutIntervalForResource=15
        let session=URLSession(configuration:config,delegate:TelegramNoRedirect(),delegateQueue:nil)
        defer{session.invalidateAndCancel()}
        do{
            let credentials:TelegramCredentials
            if telegramToken.isEmpty && telegramChatID.isEmpty && telegramSaved{credentials=try telegramStore.read()}
            else{credentials=TelegramCredentials(token:telegramToken.trimmingCharacters(in:.whitespacesAndNewlines),chatID:telegramChatID.trimmingCharacters(in:.whitespacesAndNewlines))}
            telegramUsername=try await TelegramClient.check(credentials,session:session)
            try telegramStore.save(credentials)
            if mode=="monitor" {lastMonitorDelivery=nil}
            telegramToken="";telegramChatID="";telegramSaved=true;telegramConnected=true
            telegramStatus=L("@\(telegramUsername) 与目标会话检查通过 · 已保存到本机钥匙串", "@\(telegramUsername) and target chat verified · Saved to this Mac’s Keychain")
        }catch{telegramStatus=(error as? TelegramFailure)?.errorDescription ?? L("Telegram 检查未完成，请重试", "Telegram check did not complete. Please try again.")}
    }
    var process:Process?
    var telegramStore:TelegramStore { TelegramStore(mode:mode) }
    init(){telegramSaved=telegramStore.configured;if telegramSaved{telegramStatus=L("本机已保存配置，点击连接 Telegram 重新检查", "Configuration saved on this Mac. Connect Telegram to check it again.")};Task {await startupCheck();await action("status")}}
    func startupCheck() async {
        guard !startupRunning else{return}
        startupVisible=true;startupRunning=true;startupStep=0;startupResults=[];startupNetworkOK=false
        let info=ProcessInfo.processInfo
        var load:Double=0;getloadavg(&load,1)
        startupResults.append(L("\(info.activeProcessorCount) 个可用 CPU 核心 · 1 分钟负载 \(String(format:"%.2f",load))", "\(info.activeProcessorCount) available CPU cores · 1-minute load \(String(format:"%.2f",load))"))
        startupStep=1
        let cpu=await Task.detached(priority:.utility){()->String in
            let start=ProcessInfo.processInfo.systemUptime
            var value:UInt64=0x12345678
            for _ in 0..<2_000_000 {value ^= value << 13;value ^= value >> 7;value ^= value << 17}
            let elapsed=(ProcessInfo.processInfo.systemUptime-start)*1000
            return L("200 万次整数运算：\(String(format:"%.1f",elapsed)) ms · 校验 \(value)", "2 million integer operations: \(String(format:"%.1f",elapsed)) ms · Checksum \(value)")
        }.value
        startupResults.append(cpu);startupStep=2
        let memory=await Task.detached(priority:.utility){()->String in
            var bytes=[UInt8](repeating:0,count:32*1024*1024)
            for index in stride(from:0,to:bytes.count,by:4096){bytes[index]=UInt8((index/4096)%251)}
            let valid=stride(from:0,to:bytes.count,by:4096).allSatisfy{bytes[$0]==UInt8(($0/4096)%251)}
            return valid ? L("32 MiB 分配与分页读写检查通过", "32 MiB allocation and page read/write check passed") : L("内存读写检查未通过", "Memory read/write check failed")
        }.value
        startupResults.append(L("物理内存 \(Int(info.physicalMemory/1_073_741_824)) GB · ", "Physical memory \(Int(info.physicalMemory/1_073_741_824)) GB · ")+memory);startupStep=3
        var largestDelay=0.0
        for _ in 0..<12 {
            let start=info.systemUptime
            try? await Task.sleep(nanoseconds:25_000_000)
            largestDelay=max(largestDelay,max(0,(info.systemUptime-start)*1000-25))
        }
        let thermal:String
        switch info.thermalState {case .nominal:thermal=L("正常", "Normal");case .fair:thermal=L("轻度升温", "Warm");case .serious:thermal=L("高温", "Hot");case .critical:thermal=L("严重高温", "Critical");@unknown default:thermal=L("未知", "Unknown")}
        startupResults.append(L("短时响应最大额外延迟 \(String(format:"%.1f",largestDelay)) ms · 温度状态：", "Maximum extra response delay \(String(format:"%.1f",largestDelay)) ms · Thermal state: ")+thermal);startupStep=4
        let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=5;config.timeoutIntervalForResource=6
        let session=URLSession(configuration:config)
        var request=URLRequest(url:URL(string:"https://averra-ai.haomingzhang2002.chatgpt.site/")!);request.httpMethod="HEAD"
        let start=info.systemUptime
        do {
            let (_,response)=try await session.data(for:request)
            let code=(response as? HTTPURLResponse)?.statusCode ?? 0
            startupNetworkOK=(200..<400).contains(code)
            startupResults.append((200..<400).contains(code) ? L("Averra HTTPS 可达 · \(String(format:"%.0f",(info.systemUptime-start)*1000)) ms", "Averra HTTPS reachable · \(String(format:"%.0f",(info.systemUptime-start)*1000)) ms") : L("Averra 服务响应 HTTP \(code)，连接可能受限", "Averra returned HTTP \(code). Connectivity may be limited."))
        }catch{startupResults.append(L("Averra 网络检查未完成，可离线进入；联网功能暂不可确认", "Averra network check did not complete. You can continue offline; online features are not confirmed."))}
        session.invalidateAndCancel();startupStep=5
        startupReport=startupResults.joined(separator:"\n")+L("\n\n这是轻量启动检查，不是硬件诊断、长期稳定性测试或交易就绪证明。网络检查仅访问 Averra；不连接券商、不上传性能结果。", "\n\nThis is a lightweight startup check, not hardware diagnostics, a long-term stability test, or proof of trading readiness. The network check only contacts Averra; it does not connect to a broker or upload performance results.")
        startupRunning=false
    }
    @Published var monitoring=false
    @Published var monitorTelegram=false
    @Published var monitorStatus=""
    private var monitorTask:Task<Void,Never>?
    private var lastMonitorDelivery:String?
    func startMonitoring() {
        guard mode=="monitor",ready,!busy,!monitoring else{return}
        monitoring=true
        monitorStatus=L("每 5 分钟检查 QQQ / SMH；关闭 App 后停止。", "Checking QQQ / SMH every 5 minutes; stops when the app closes.")
        monitorTask=Task {
            while !Task.isCancelled && monitoring {
                if !busy {await action("monitor_cycle")}
                do {try await Task.sleep(nanoseconds:300_000_000_000)} catch {break}
            }
        }
    }
    func stopMonitoring() {
        monitoring=false;monitorTask?.cancel();monitorTask=nil
        if process?.isRunning==true {process?.terminate()}
        monitorStatus=L("持续监控已停止。", "Continuous monitoring stopped.")
    }
    func action(_ action:String) async {
        if busy{return};busy=true
        let payload:[String:Any]=["action":action,"symbol":symbol,"qqq":qqq/100,"language":AppLanguage.code,"source":stockSource]
        let resources=self.resources,workspace=self.workspace,mode=self.mode
        do {
            let data=try JSONSerialization.data(withJSONObject:payload)
            let result=try await withCheckedThrowingContinuation { (continuation:CheckedContinuation<[String:Any],Error>) in
                let task=Process();process=task
                task.executableURL=resources.appendingPathComponent("Python.framework/Versions/3.14/bin/python3.14")
                task.arguments=[resources.appendingPathComponent(mode=="stock" ? "stock_bridge.py" : "bridge.py").path,resources.path,workspace.path,mode]
                var env=["PYTHONHOME":resources.appendingPathComponent("Python.framework/Versions/3.14").path,"PYTHONNOUSERSITE":"1","PYTHONDONTWRITEBYTECODE":"1","PYTHONUNBUFFERED":"1","HOME":FileManager.default.homeDirectoryForCurrentUser.path,"PATH":"/usr/bin:/bin:/usr/sbin:/sbin","SSL_CERT_FILE":resources.appendingPathComponent("cert.pem").path]
                if let temp=ProcessInfo.processInfo.environment["TMPDIR"]{env["TMPDIR"]=temp}
                if mode=="agent"{env["CODEX_HOME"]=self.mcpHome}
                task.environment=env
                let input=Pipe(),output=Pipe(),errors=Pipe();task.standardInput=input;task.standardOutput=output;task.standardError=errors
                task.terminationHandler={_ in
                    let bytes=output.fileHandleForReading.readDataToEndOfFile()
                    do{let parsed=try JSONSerialization.jsonObject(with:bytes) as? [String:Any] ?? [:];continuation.resume(returning:parsed)}catch{continuation.resume(throwing:error)}
                }
                do{try task.run();input.fileHandleForWriting.write(data);try input.fileHandleForWriting.close()}catch{continuation.resume(throwing:error)}
            }
            if action=="monitor_cycle",!Task.isCancelled,monitoring,monitorTelegram,
               (result["exitCode"] as? Int)==0, let key=result["notification_key"] as? String,
               key != lastMonitorDelivery, let message=result["output"] as? String {
                do {
                    let credentials=try telegramStore.read()
                    let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=20;config.timeoutIntervalForResource=25
                    let session=URLSession(configuration:config,delegate:TelegramNoRedirect(),delegateQueue:nil)
                    defer {session.invalidateAndCancel()}
                    try await TelegramClient.send(message,credentials:credentials,session:session)
                    lastMonitorDelivery=key
                    monitorStatus=L("Telegram 已送达；信号不变时不重复发送。", "Telegram delivered; unchanged signals are not resent.")
                }catch {if monitoring {monitorStatus=L("Telegram 未确认送达，下一轮重试。", "Telegram delivery unconfirmed; retrying next cycle.")}}
            }
            if let error=result["error"] as? String{resultTime=Date();recordDate="";resultExplanation="";resultState="failed";log=error;status=L("操作未完成", "Operation did not complete")}
            else if action=="status"{ready=result["ready"] as? Bool ?? false;status=mode=="stock" ? (ready ? L("已验证 · 本地只读模式", "Verified · Local read-only mode") : L("请验证你的 Stock 密钥", "Verify your Stock key")) : ready ? L("已验证 · \(mode == "agent" ? "只读检查" : "仅监控")", "Verified · \(mode == "agent" ? "Read-only checks" : "Monitor only")") : L("请验证你的 \(mode == "agent" ? "Agent" : "Monitor") 密钥", "Verify your \(mode == "agent" ? "Agent" : "Monitor") key")}
            else{resultTime=Date();recordDate=result["record_date"] as? String ?? "";resultExplanation=result["explanation"] as? String ?? "";resultState=(result["exitCode"] as? Int ?? 0) != 0 ? "failed" : action=="demo" ? "demo" : result["summary_state"] as? String ?? "complete";if action=="mcp_connect"{mcpConnected=(result["exitCode"] as? Int ?? 1)==0};log=result["output"] as? String ?? L("完成", "Done");status=(result["exitCode"] as? Int ?? 0)==0 ? L("已完成", "Completed") : L("检查未通过，请查看结果", "Check failed. See the results.")}
        }catch{resultTime=Date();recordDate="";resultExplanation="";resultState="failed";log=error.localizedDescription;status=L("操作未完成", "Operation did not complete")}
        process=nil;busy=false
    }
    func verify() async {
        guard !busy else{return}
        let value=key.trimmingCharacters(in:.whitespacesAndNewlines),prefix=keyPrefix
        guard value.hasPrefix(prefix),value.count==70 else{status=L("密钥格式不正确，请使用对应模式的密钥", "Invalid key format. Use the key for this app’s mode.");return}
        busy=true;status=L("正在安全验证密钥", "Verifying your key securely")
        defer{busy=false;key=""}
        do {
            var request=URLRequest(url:URL(string:"https://averra-ai.haomingzhang2002.chatgpt.site/api/setup-key/verify")!)
            request.httpMethod="POST";request.timeoutInterval=20;request.setValue("application/json",forHTTPHeaderField:"Content-Type")
            request.httpBody=try JSONSerialization.data(withJSONObject:["mode":mode,"key":value])
            let (data,response)=try await URLSession.shared.data(for:request)
            let result=try? JSONSerialization.jsonObject(with:data) as? [String:Any]
            guard (response as? HTTPURLResponse)?.statusCode==200,result?["ok"] as? Bool==true else{status=L("密钥未通过或验证服务暂不可用，没有启用运行", "Key verification failed or the service is unavailable. Running has not been enabled.");return}
            let receipt=try JSONSerialization.data(withJSONObject:["mode":mode,"verifiedAt":ISO8601DateFormatter().string(from:Date())])
            let target=workspace.appendingPathComponent(".desktop-verified.json");try receipt.write(to:target,options:.atomic)
            try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:target.path)
            ready=true;status=L("验证成功 · 不会自动开始运行", "Verified · Will not start automatically");log=L("密钥已验证，不保存密钥明文。配置与日志只保存在此 App 的独立工作区。", "Key verified; the plaintext key is not stored. Settings and logs stay in this app’s separate workspace.")
        }catch{status=L("无法验证，请检查网络后重试", "Verification failed. Check your connection and try again.");log=L("未保存密钥，未启动任何交易。", "Key not saved. No trading was started.")}
    }
    func openWorkspace(){NSWorkspace.shared.open(workspace)}
    func openGuide(){let path=mode == "stock" ? "/agent/guide/?agent=stock" : mode == "agent" ? "/agent/guides/README_AGENT_\(AppLanguage.isChinese ? "ZH" : "EN").html" : "/agent/guides/README_MONITOR_\(AppLanguage.isChinese ? "ZH" : "EN").html";NSWorkspace.shared.open(URL(string:"https://averra-ai.haomingzhang2002.chatgpt.site"+path)!)}
}
struct AverraButton:ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var primary=false
    func makeBody(configuration:Configuration)->some View {
        configuration.label.font(.system(size:13,weight:.semibold)).padding(.horizontal,16).padding(.vertical,11)
            .foregroundStyle(primary ? Color.white : Color(red:0.24,green:0.19,blue:0.22))
            .background(primary ? (Bundle.main.object(forInfoDictionaryKey:"AverraMode") as? String == "stock" ? Color(red:0.33,green:0.27,blue:0.56) : Color(red:0.32,green:0.23,blue:0.28)) : Color.white)
            .clipShape(RoundedRectangle(cornerRadius:10)).overlay(RoundedRectangle(cornerRadius:10).stroke(Color.black.opacity(primary ? 0 : 0.08)))
            .opacity(isEnabled ? 1 : 0.38).scaleEffect(configuration.isPressed ? 0.97 : 1).animation(.easeOut(duration:0.16),value:configuration.isPressed)
    }
}
struct ContentView:View {
    @ObservedObject var model:Model
    @State private var section="overview"
    @Namespace private var startupMark
    var pink:Color {model.mode=="stock" ? Color(red:0.49,green:0.43,blue:0.82) : Color(red:0.87,green:0.57,blue:0.72)}
    let ink=Color(red:0.22,green:0.17,blue:0.20)
    var body:some View {
        HStack(spacing:0){
            VStack(alignment:.leading,spacing:32){
                HStack(spacing:10){Group{if !model.startupVisible{Image(nsImage:NSImage(contentsOf:model.resources.appendingPathComponent("mark.png")) ?? NSImage()).resizable().scaledToFit().matchedGeometryEffect(id:"startupMark",in:startupMark)}else{Color.clear}}.frame(width:28,height:28);Text("Averra").font(.system(size:24,weight:.semibold))}
                VStack(alignment:.leading,spacing:8){
                    nav(L("总览", "Overview"),icon:"square.grid.2x2",id:"overview")
                    nav(L("连接与设置", "Settings"),icon:"slider.horizontal.3",id:"settings")
                    nav(L("运行结果", "Results"),icon:"doc.text",id:"results")
                }
                Spacer()
                VStack(alignment:.leading,spacing:12){
                    Text(model.mode=="stock" ? "STOCK" : model.mode=="agent" ? "AGENT" : "MONITOR").font(.system(size:11,weight:.semibold)).tracking(2)
                    Text(model.mode=="agent" ? L("只读验证模式", "Read-only validation") : L("纯监控模式", "Monitor only")).font(.system(size:14,weight:.medium))
                    Text(model.mode=="monitor" ? "V20261011.1" : "V20261011").font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
                    Divider()
                    Button(L("使用说明", "User Guide")){model.openGuide()}.buttonStyle(.plain)
                    Button(L("打开工作区", "Open Workspace")){model.openWorkspace()}.buttonStyle(.plain)
                }.font(.system(size:12)).foregroundStyle(ink)
            }.padding(24).frame(width:190).frame(maxHeight:.infinity).background((model.mode=="stock" ? Color(red:0.93,green:0.92,blue:0.98) : Color(red:0.97,green:0.91,blue:0.94)))
            ScrollView{
                VStack(alignment:.leading,spacing:24){
                    HStack(alignment:.top){
                        VStack(alignment:.leading,spacing:8){
                            Text(section=="settings" ? L("连接与设置", "Connections & Settings") : section=="results" ? L("运行结果", "Results") : model.name).font(.system(size:32,weight:.semibold))
                            Text(model.mode=="agent" ? L("先连接，再安心检查", "Connect first, then check with confidence") : L("关注市场，保留你的判断", "Follow the market. Keep your own judgment.")).font(.system(size:15)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
                        }
                        Spacer()
                        HStack(spacing:7){Circle().fill(model.busy ? pink : model.ready ? ink : .gray).frame(width:6,height:6);Text(model.busy ? L("处理中", "Working") : model.ready ? L("已启用", "Enabled") : L("待验证", "Verify first")).font(.caption)}.padding(.horizontal,12).padding(.vertical,8).background(Color.white).clipShape(Capsule())
                    }
                    if section=="settings" { setupProgress; if !model.ready { activation } }
                    if section=="overview" && !model.ready { nextStep }
                    if section=="overview" && model.mode=="stock" { stockOverview }
                    if section=="overview" && model.mode != "stock" {
                        card{
                            VStack(alignment:.leading,spacing:18){
                                Text(model.mode=="agent" ? L("你的账户检查助手", "Your account check assistant") : L("QQQ 与 SMH，一处查看", "QQQ and SMH in one place")).font(.system(size:22,weight:.medium))
                                Text(model.mode=="agent" ? L("配置检查、MCP 连接与只读验证集中在这里。不会自动运行，也没有实盘启动入口。", "Configuration checks, MCP connection, and read-only validation in one place. No automatic runs or live-trading start button.") : L("查看 WAIT / SMALL BUY / LARGE BUY。启动持续监控后每 5 分钟检查两只 ETF，可开启 Telegram 信号通知；不连接券商、不下单。", "See WAIT / SMALL BUY / LARGE BUY. Start continuous monitoring to check both ETFs every 5 minutes, with optional Telegram signals. No brokerage connection or orders.")).font(.system(size:14)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).lineSpacing(5)
                                if model.mode=="monitor" {
                                    Toggle(L("发送 Telegram 信号通知", "Send Telegram signal notifications"),isOn:$model.monitorTelegram).disabled(!model.telegramSaved)
                                    HStack {
                                        Button(model.monitoring ? L("停止持续监控", "Stop Monitoring") : L("启动持续监控", "Start Monitoring")) {
                                            if model.monitoring {model.stopMonitoring()} else {model.startMonitoring()}
                                        }.buttonStyle(AverraButton(primary:true)).disabled(!model.ready || (!model.monitoring && model.busy))
                                        Text(L("每 5 分钟 · 保持 App 打开", "Every 5 minutes · Keep app open")).font(.caption)
                                    }
                                    Text(model.monitorStatus).font(.caption).textSelection(.enabled)
                                }
                                HStack{Button(model.mode=="agent" ? L("运行只读验证", "Run Read-only Check") : L("运行一次监控", "Run Monitor Once")){run("run")}.buttonStyle(AverraButton(primary:true)).disabled(!model.ready || model.busy || (model.mode=="stock" && model.stockSource.isEmpty));if model.ready {Button(L("离线示例", "Offline Example")){run("demo")}.buttonStyle(AverraButton()).disabled(model.busy)}}
                            }
                        }
                        HStack(spacing:16){
                            if model.mode=="monitor" {card{VStack(alignment:.leading,spacing:10){Text(L("三档市场信号", "Three market signals")).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text("WAIT · SMALL BUY · LARGE BUY").font(.system(size:16,weight:.medium));Text(L("反映市场条件，不代表买入金额或账户比例。", "Market conditions, not purchase amounts or account allocations.")).font(.caption)}}} else {
                            card{VStack(alignment:.leading,spacing:10){Text(L("参考分配", "Reference Allocation")).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text("SMH 70% · QQQ 20% · SGOV 10%").font(.system(size:18,weight:.medium));Button(L("调整设置", "Adjust Settings")){withAnimation(.easeInOut(duration:0.22)){section="settings"}}.buttonStyle(.plain).foregroundStyle(pink)}}
                            }
                            card{VStack(alignment:.leading,spacing:10){Text(L("连接状态", "Connection Status")).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text(model.mode=="agent" ? (model.mcpConnected ? L("MCP 已连接", "MCP connected") : L("MCP 待检查", "Check MCP")) : (model.monitoring ? L("持续监控中", "Monitoring active") : L("按需获取行情", "Market data on demand"))).font(.system(size:18,weight:.medium));Text(model.mode=="monitor" && model.monitoring ? L("已完成日线 · 每 5 分钟检查", "Completed daily bars · Checks every 5 minutes") : L("不会自动开始运行", "Will not start automatically")).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}}
                        }
                    }
                    if section=="settings" {

                        if model.mode=="agent" { mcp }
                        if model.mode=="stock" { stockConnection }
                        if model.mode=="monitor" {card{VStack(alignment:.leading,spacing:14){
                            Text(L("监控设置", "Monitoring Setup")).font(.headline)
                            Text(L("持续监控同时检查 QQQ 和 SMH，使用已完成日线，不需要购买力或持仓。", "Continuous monitoring checks both QQQ and SMH using completed daily bars. Buying Power and holdings are not required.")).font(.system(size:14))
                            Picker(L("单次检查标的", "Single-check symbol"),selection:$model.symbol){Text("QQQ").tag("QQQ");Text("SMH").tag("SMH")}.pickerStyle(.segmented)
                            Button(L("检查配置", "Check Configuration")){run("check")}.buttonStyle(AverraButton()).disabled(!model.ready || model.busy)
                        }}}
                        if model.mode == "agent" {card{
                            VStack(alignment:.leading,spacing:18){
                                Text(L("参考分配", "Reference Allocation")).font(.headline)
                                Text("SMH 70% · QQQ 20% · SGOV 10%").font(.system(size:18,weight:.medium))
                                Text(L("回调：SGOV 最多 20%；复苏：SGOV 最多 5%。只有 SGOV 可以卖出。公开 App 只做配置和账户验证。", "Pullback: SGOV up to 20%; recovery: SGOV at most 5%. Only SGOV can be sold. The public app validates configuration and accounts only.")).font(.caption)
                                Button(L("检查配置", "Check Configuration")){run("check")}.buttonStyle(AverraButton()).disabled(!model.ready || model.busy)
                            }
                        }
                    }
                    }
                    if section=="settings" {
                        DisclosureGroup(L("可选：连接 Telegram", "Optional: Connect Telegram")){telegram}
                        card{VStack(alignment:.leading,spacing:12){Text(L("启动检查", "Startup Checks")).font(.headline);Text(model.startupRunning ? L("正在检查，可继续使用离线示例", "Checking. You can still try the offline example.") : model.startupNetworkOK ? L("启动检查已完成 · Averra 网站可访问", "Startup checks completed · Averra is reachable") : L("网络尚未确认 · 离线示例仍可使用", "Network not confirmed · Offline example is available")).font(.system(size:14));DisclosureGroup(L("高级诊断", "Advanced Diagnostics")){VStack(alignment:.leading,spacing:12){Text(model.startupReport).font(.system(size:13)).textSelection(.enabled);Button(L("重新检测", "Check Again")){Task{await model.startupCheck()}}.buttonStyle(AverraButton()).disabled(model.busy || model.startupRunning)}}}}
                    }
                    if section=="results" {
                        resultSummary
                        card{
                            VStack(alignment:.leading,spacing:16){
                                HStack{Text(L("详细输出", "Detailed Output")).font(.headline);Spacer();Button(L("复制", "Copy")){NSPasteboard.general.clearContents();NSPasteboard.general.setString(model.log,forType:.string)}.buttonStyle(.plain).disabled(model.log.isEmpty)}
                                Text(model.log.isEmpty ? L("尚未运行。可以先试试离线示例，不会连接账户。", "Nothing has run yet. Try the offline example first; it will not connect to your account.") : model.log).font(.system(size:13,design:.monospaced)).textSelection(.enabled).lineSpacing(4).frame(maxWidth:.infinity,alignment:.leading)
                            }
                        }
                    }
                    HStack(spacing:8){if model.busy{ProgressView().controlSize(.small)};Text(model.status).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}
                    Text(L("独立工作区 · 不安装后台任务 · Apple Silicon", "Separate workspace · No background tasks · Apple Silicon")).font(.system(size:11)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
                }.padding(32).frame(maxWidth:850,alignment:.leading)
            }.frame(maxWidth:.infinity).background(Color(red:0.98,green:0.97,blue:0.97))
        }.allowsHitTesting(!model.startupVisible).accessibilityHidden(model.startupVisible).overlay{
            if model.startupVisible { StartupView(model:model,markNamespace:startupMark) }
        }.foregroundStyle(ink).preferredColorScheme(.light).onDisappear{if model.mode=="monitor" {model.stopMonitoring()} else {model.process?.terminate()}}
    }
    func nav(_ title:String,icon:String,id:String)->some View {
        Button{withAnimation(.easeInOut(duration:0.22)){section=id}}label:{HStack(spacing:11){Image(systemName:icon).frame(width:18);Text(title);Spacer()}.font(.system(size:14,weight:section==id ? .semibold : .regular)).padding(12).background(section==id ? Color.white.opacity(0.85) : Color.clear).clipShape(RoundedRectangle(cornerRadius:10))}.buttonStyle(.plain)
    }
    func card<C:View>(@ViewBuilder content:()->C)->some View {content().frame(maxWidth:.infinity,alignment:.leading).padding(22).background(Color.white).clipShape(RoundedRectangle(cornerRadius:16)).overlay(RoundedRectangle(cornerRadius:16).stroke(Color.black.opacity(0.05))).shadow(color:Color.black.opacity(0.025),radius:12,y:5)}
    func run(_ action:String){Task{await model.action(action);if ["run","check","demo"].contains(action){withAnimation(.easeInOut(duration:0.22)){section="results"}}}}
    var nextStep:some View {card{VStack(alignment:.leading,spacing:14){
        Text(model.ready ? L("下一步", "Your next step") : L("先看看 Averra 能为你展示什么", "See what Averra can show you")).font(.headline)
        Text(model.ready ? (model.mode=="stock" && model.stockSource.isEmpty ? L("选择已有的监控目录，即可读取保存的记录。", "Choose your existing monitor folder to read saved records.") : L("设置完成后，手动运行一次检查并查看结果。", "Once setup is complete, run a check and review the result.")) : L("离线示例使用合成数据，无需密钥，也不连接账户。准备使用自己的数据时，再开始设置。", "The offline example uses synthetic data. No key or account connection is required. Start setup when you are ready to use your own data.")).font(.system(size:14)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).lineSpacing(4)
        HStack{Button(L("查看离线示例", "Preview Offline Example")){run("demo")}.buttonStyle(AverraButton(primary:true)).disabled(model.busy);Button(model.ready ? L("查看设置", "Review Setup") : L("开始设置", "Start Setup")){section="settings"}.buttonStyle(AverraButton())}
    }}}
    var setupProgress:some View {card{VStack(alignment:.leading,spacing:12){
        Text(L("完成必要设置，再查看结果", "Set up, then view your results")).font(.headline)
        Label(model.ready ? L("1 · 密钥已验证", "1 · Key verified") : L("1 · 验证对应模式的密钥", "1 · Verify the key for this app"),systemImage:model.ready ? "checkmark.circle" : "1.circle")
        Label(model.mode=="stock" ? (model.stockSource.isEmpty ? L("2 · 选择已有 Stock 监控目录", "2 · Choose your existing Stock folder") : L("2 · 监控目录已选择", "2 · Monitor folder selected")) : model.mode=="agent" ? (model.mcpConnected ? L("2 · MCP 连接已检查", "2 · MCP connection checked") : L("2 · 检查已有 MCP 连接", "2 · Check your existing MCP connection")) : L("2 · 按需连接 Telegram，或直接查看市场信号", "2 · Optionally connect Telegram, or view market signals directly"),systemImage:"2.circle")
        Label(model.mode=="monitor" ? L("3 · 在总览启动持续监控，前往结果页查看两只 ETF", "3 · Start monitoring on Overview; view both ETFs in Results") : L("3 · 在总览手动运行，前往结果页查看", "3 · Run from Overview, then open Results"),systemImage:"3.circle")
        Text(L("Telegram 是可选连接，不影响离线示例。", "Telegram is optional and is not needed for the offline example.")).font(.system(size:13)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
    }.font(.system(size:14))}}
    var resultSummary:some View {card{VStack(alignment:.leading,spacing:14){
        Text(model.log.isEmpty ? L("还没有结果", "No results yet") : model.resultState=="demo" ? L("离线示例 · 合成数据", "Offline example · Synthetic data") : model.resultState=="failed" ? L("本次读取或检查未完成", "This read or check did not complete") : model.resultState=="empty" ? L("尚无可用监控记录", "No monitor records available") : model.resultState=="stale" ? L("保存的记录已超过七天", "Saved records are over seven days old") : model.resultState=="incomplete" ? L("部分本地记录不可用", "Some local records are unavailable") : L("结果已生成", "Result available")).font(.system(size:22,weight:.medium))
        Text(model.log.isEmpty ? L("先查看离线示例，了解输出内容；使用自己的数据前，请完成设置。", "Preview an offline example to understand the output. Complete setup before using your own data.") : model.resultState=="demo" ? L("这是本机运行的示例，不是实时行情或你的实际持仓。建议不会自动变成持仓，也不会产生订单。", "This example runs locally. It is not live market data or your actual holdings. Suggestions do not become holdings or orders automatically.") : model.resultState=="failed" ? L("查看下方详细输出，并在设置中检查密钥和数据来源后重试。", "Review the details below, then check your key and data source in Settings before retrying.") : model.mode=="stock" ? L("这里只展示已保存的记录。请核对记录日期；读取成功不代表新的市场扫描已经完成。", "These are saved records. Check their date; a successful read does not mean a new market scan has completed.") : L("请阅读详细输出中的状态、原因与数据时间。检查完成不代表允许下单。", "Read the status, reason and data time in the details. A completed check is not permission to trade.")).font(.system(size:14)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).lineSpacing(4)
        if !model.resultExplanation.isEmpty {Text(model.resultExplanation).font(.system(size:15,weight:.medium)).lineSpacing(5)}
        if !model.recordDate.isEmpty {Text(L("记录日期：", "Record date: ")+model.recordDate).font(.system(size:13))}
        if let time=model.resultTime {Text(L("本次运行：", "This run: ")+time.formatted(date:.abbreviated,time:.shortened)).font(.system(size:13)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}
        HStack{Button(L("查看离线示例", "Preview Offline Example")){run("demo")}.buttonStyle(AverraButton(primary:true)).disabled(model.busy);Button(L("检查设置", "Review Settings")){section="settings"}.buttonStyle(AverraButton())}
    }}}
    var stockOverview:some View {VStack(alignment:.leading,spacing:16){
        card{VStack(alignment:.leading,spacing:18){
            Text(L("看清信号，再做决定", "Understand the signal. Make your own decision.")).font(.system(size:22,weight:.medium))
            Text(L("普通股研究与持仓监控，和 ETF 完全分开。本 App 读取本地已完成的监控结果；不会发起付费 AI 研究、全市场扫描或交易。", "Stock research and holdings monitoring, separate from ETF. This app reads completed local monitor results; it does not start paid AI research, market-wide scans, or trades.")).font(.system(size:14)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).lineSpacing(5)
            HStack{Button(L("读取监控状态", "Read Monitor Status")){run("run")}.buttonStyle(AverraButton(primary:true)).disabled(!model.ready || model.busy || (model.mode=="stock" && model.stockSource.isEmpty));if model.ready {Button(L("离线示例", "Offline Example")){run("demo")}.buttonStyle(AverraButton()).disabled(model.busy)}}
        }}
        HStack(spacing:16){
            card{VStack(alignment:.leading,spacing:10){Text(L("工作方式", "How It Works")).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text(L("只监控，不下单", "Monitor only. No orders.")).font(.system(size:18,weight:.medium));Text(L("持仓以用户确认记录为准", "Holdings follow user confirmations")).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}}
            card{VStack(alignment:.leading,spacing:10){Text(L("本地数据", "Local Data")).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text(model.stockSource.isEmpty ? L("尚未选择工程目录", "No project folder selected") : L("已选择 · 只读", "Selected · Read only")).font(.system(size:18,weight:.medium));Button(L("选择监控目录", "Choose Monitor Folder")){model.chooseStockSource()}.buttonStyle(.plain).foregroundStyle(pink)}}
        }
    }}
    var stockConnection:some View {card{VStack(alignment:.leading,spacing:14){
        Text(L("本地监控工程", "Local Monitor Project")).font(.headline)
        Text(L("选择已有 Stock Agent 目录，读取持仓、最近监控状态和通知送达记录。不会启动、重启或修改后台服务，也不读取 .env 中的密钥。", "Choose an existing Stock Agent folder to read holdings, recent monitor state, and delivery records. No background service is started, restarted, or changed; credentials in .env are not read.")).font(.system(size:13)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
        if !model.stockSource.isEmpty{Text(model.stockSource).font(.system(size:11,design:.monospaced)).textSelection(.enabled)}
        HStack{Button(L("选择监控目录", "Choose Monitor Folder")){model.chooseStockSource()}.buttonStyle(AverraButton(primary:true));Button(L("读取监控状态", "Read Monitor Status")){run("run")}.buttonStyle(AverraButton()).disabled(!model.ready || model.busy || model.stockSource.isEmpty)}
    }}}
    var activation:some View {card{VStack(alignment:.leading,spacing:12){Text(L("启用你的 \(model.keyModeName)", "Enable your \(model.keyModeName)")).font(.headline);HStack{SecureField(L("输入 \(model.keyPrefix) 密钥", "Enter \(model.keyPrefix) key"),text:$model.key).textFieldStyle(.roundedBorder);Button(L("验证密钥", "Verify Key")){Task{await model.verify()}}.buttonStyle(AverraButton(primary:true)).disabled(model.busy)};Text(L("密钥仅提交至 Averra 验证服务，不保存明文", "Your key is sent only to Averra for verification; plaintext is not saved.")).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}}}
    var telegram:some View {card{VStack(alignment:.leading,spacing:14){
        HStack{Text("Telegram").font(.headline);Spacer();Text(model.telegramConnected ? L("已连接", "Connected") : (model.telegramSaved ? L("已保存 · 待检查", "Saved · Check required") : L("待连接", "Not connected"))).foregroundStyle(model.telegramConnected ? pink : .secondary)}
        Text(L("1  在 BotFather 创建或找到你的机器人", "1  Create or find your bot in BotFather")).font(.system(size:13,weight:.medium))
        Button(L("打开 BotFather", "Open BotFather")){NSWorkspace.shared.open(URL(string:"https://t.me/BotFather")!)}.buttonStyle(.plain).foregroundStyle(pink)
        Text(L("2  在 Telegram 给机器人发送 /start；群组使用时先加入机器人", "2  Send /start to your bot in Telegram; add the bot first when using a group.")).font(.system(size:13,weight:.medium))
        Text(L("3  填入 Bot Token 和目标 Chat ID，或导入已有配置", "3  Enter your Bot Token and target Chat ID, or import existing settings.")).font(.system(size:13,weight:.medium))
        SecureField(model.telegramSaved ? L("Bot Token 已存钥匙串，留空可复用", "Bot Token saved in Keychain; leave blank to reuse") : "Bot Token",text:$model.telegramToken).textFieldStyle(.roundedBorder).onChange(of:model.telegramToken){_ in model.telegramConnected=false}
        SecureField(model.telegramSaved ? L("Chat ID 已保存，留空可复用", "Chat ID saved; leave blank to reuse") : L("Chat ID（群组通常为负数）", "Chat ID (usually negative for groups)"),text:$model.telegramChatID).textFieldStyle(.roundedBorder).onChange(of:model.telegramChatID){_ in model.telegramConnected=false}
        HStack{Button(model.telegramChecking ? L("正在检查…", "Checking…") : L("连接 Telegram", "Connect Telegram")){Task{await model.connectTelegram()}}.buttonStyle(AverraButton(primary:true)).disabled(!model.ready || model.busy || model.telegramChecking);Button(L("导入已有配置", "Import Settings")){model.importTelegram()}.buttonStyle(AverraButton()).disabled(model.busy || model.telegramChecking)}
        Text(model.telegramStatus).font(.system(size:12)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
        if model.mode=="monitor" {Text(L("如需通知，请在概览中开启 Telegram 信号通知并启动持续监控。", "For notifications, enable Telegram signal notifications on Overview and start monitoring.")).font(.caption)}
        Text(L("点击连接会把 Token / Chat ID 通过 HTTPS 提交给 Telegram，并在验证成功后保存到本机钥匙串。连接验证仅检查身份与会话，不发送消息、不读取消息队列，也不会修改后台 Agent 配置。", "Connect sends Token / Chat ID to Telegram over HTTPS and saves them to this Mac’s Keychain after verification. Connection verification only checks identity and chat access. It does not send messages, read the message queue, or change the background service.")).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).lineSpacing(3)
    }}}
    var mcp:some View {card{VStack(alignment:.leading,spacing:14){HStack{Text("Robinhood MCP").font(.headline);Spacer();Text(model.mcpConnected ? L("已连接", "Connected") : L("待连接", "Not connected")).foregroundStyle(model.mcpConnected ? pink : .secondary)};Text(L("复用 Codex 中已有的登录授权。连接检查不读取账户或提交订单。", "Reuse your existing Codex login. The connection check does not read your account or submit orders.")).font(.system(size:13)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40));Text(model.mcpHome).font(.system(size:11,design:.monospaced)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).textSelection(.enabled);HStack{Button(L("连接 MCP", "Connect MCP")){run("mcp_connect")}.buttonStyle(AverraButton(primary:true)).disabled(!model.ready || model.busy);Button(L("选择配置目录", "Choose Config Folder")){model.chooseMCP()}.buttonStyle(AverraButton()).disabled(model.busy)};Text(L("首次授权请在 Codex 中完成 Robinhood MCP 登录", "Complete the first Robinhood MCP login in Codex.")).font(.caption).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))}}}
}

struct StartupView:View {
    @ObservedObject var model:Model
    let markNamespace:Namespace.ID
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expanded=false
    @State private var gathered=false
    @State private var exiting=false
    @State private var skipped=false
    @State private var firstLaunch=true
    @State private var pulse=false
    var pink:Color {model.mode=="stock" ? Color(red:0.49,green:0.43,blue:0.82) : Color(red:0.87,green:0.57,blue:0.72)}
    let labels=["CPU",L("内存", "Memory"),L("响应", "Response"),L("网络", "Network")]
    let icons=["cpu","memorychip","waveform.path.ecg","network"]
    let thresholds=[2,3,4,5]
    var body:some View {
        GeometryReader{geometry in
            ZStack{
                Color(red:0.98,green:0.97,blue:0.97).opacity(exiting ? 0 : 1).ignoresSafeArea()
                ZStack{
                        ForEach(0..<4,id:\.self){index in
                            let angle=Double(index)*Double.pi/2-Double.pi/4
                            let radius:Double=expanded && !gathered ? 108 : 0
                            Path{path in
                                path.move(to:CGPoint(x:160,y:145))
                                path.addLine(to:CGPoint(x:160+cos(angle)*radius,y:145+sin(angle)*radius))
                            }.trim(from:0,to:expanded && !gathered ? 1 : 0).stroke(pink.opacity(0.25),style:StrokeStyle(lineWidth:1,dash:[3,5]))
                            ZStack{
                                    Circle().fill(model.startupStep>=thresholds[index] ? pink.opacity(0.22) : Color.white).frame(width:42,height:42)
                                    Image(systemName:model.startupStep>=thresholds[index] ? (index==3 && !model.startupNetworkOK ? "exclamationmark" : "checkmark") : icons[index]).font(.system(size:16,weight:.medium)).foregroundStyle(pink)
                                Text(labels[index]).font(.system(size:11,weight:.medium)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).offset(y:35).opacity(expanded && !gathered ? 1 : 0)
                            }.offset(x:cos(angle)*radius,y:sin(angle)*radius).opacity(gathered ? 0 : 1)
                        }
                }.frame(width:320,height:290).position(x:geometry.size.width/2,y:geometry.size.height/2-62).opacity(exiting ? 0 : 1).accessibilityHidden(true)
                VStack(spacing:18){
                    Text(gathered ? (model.startupRunning ? L("可以进入了，检测仍在继续", "You can continue. Checks are still running.") : model.startupNetworkOK ? L("准备好了，我们开始吧", "Ready. Let’s get started.") : L("已准备好，可离线进入", "Ready. You can continue offline.")) : L("从一个起点，连接更多可能", "One starting point. More possibilities.")).font(.system(size:25,weight:.semibold))
                    Text(model.name).font(.system(size:14)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
                    Text(statusText).font(.system(size:12)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).frame(height:20)
                }.position(x:geometry.size.width/2,y:geometry.size.height/2+125).opacity(exiting ? 0 : 1)
                VStack{Spacer();HStack{
                        Text(L("轻量启动检查 · 不连接券商 · 不上传检测结果", "Lightweight startup checks · No broker connection · No results uploaded")).font(.system(size:11)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40))
                        Spacer()
                        Button(L("跳过动画", "Skip Animation")){skipped=true;finish()}.buttonStyle(.plain).font(.system(size:12)).foregroundStyle(Color(red:0.40,green:0.37,blue:0.40)).disabled(exiting)
                    }.padding(28)
                }.opacity(exiting ? 0 : 1)
                Group{
                    if gathered{
                        Image(nsImage:NSImage(contentsOf:model.resources.appendingPathComponent("mark.png")) ?? NSImage()).resizable().scaledToFit().matchedGeometryEffect(id:"startupMark",in:markNamespace)
                    }else{
                        Circle().fill(pink).overlay(Circle().stroke(Color.white.opacity(0.7),lineWidth:2))
                    }
                }.frame(width:exiting ? 28 : (expanded ? 42 : 12),height:exiting ? 28 : (expanded ? 42 : 12))
                    .scaleEffect(!exiting && !gathered && pulse ? 1.04 : 1)
                    .position(x:exiting ? 38 : geometry.size.width/2,y:exiting ? 38 : geometry.size.height/2-62)
            }
        }.onAppear{
            if !reduceMotion{withAnimation(.easeInOut(duration:1).repeatForever(autoreverses:true)){pulse=true}}
        }.task{
            firstLaunch = !UserDefaults.standard.bool(forKey:"hasSeenOpening")
            if reduceMotion{expanded=true;await waitAndFinish();return}
            if model.mode=="stock"{
                try? await Task.sleep(nanoseconds:400_000_000)
                guard !Task.isCancelled else{return}
                withAnimation(.timingCurve(0.4,0,0.2,1,duration:0.8)){expanded=true}
                try? await Task.sleep(nanoseconds:3_050_000_000)
                guard !Task.isCancelled else{return}
                await waitAndFinish();return
            }
            try? await Task.sleep(nanoseconds:firstLaunch ? 400_000_000 : 100_000_000)
            guard !Task.isCancelled else{return}
            withAnimation(.timingCurve(0.4,0,0.2,1,duration:firstLaunch ? 0.8 : 0.35)){expanded=true}
            try? await Task.sleep(nanoseconds:firstLaunch ? 1_600_000_000 : 550_000_000)
            guard !Task.isCancelled else{return}
            await waitAndFinish()
        }.animation(.easeInOut(duration:0.25),value:model.startupStep)
    }
    var statusText:String{
        if gathered{return model.startupRunning ? L("检测仍在进行，结果会保留在设置中", "Checks are still running. Results will be saved in Settings.") : L("检测报告已保留在连接与设置中", "Your check report is in Connections & Settings.")}
        if model.startupStep>=5{return model.startupResults.last ?? L("检测已完成", "Checks completed")}
        let pending=[L("正在读取机器信息", "Reading machine information"),L("正在检查 CPU", "Checking CPU"),L("正在检查内存", "Checking memory"),L("正在检查短时响应", "Checking short-term response"),L("正在检查网络", "Checking network")]
        return pending[min(model.startupStep,4)]
    }
    func waitAndFinish() async {
        while model.mode != "stock" && model.startupRunning && !Task.isCancelled && !skipped{try? await Task.sleep(nanoseconds:80_000_000)}
        guard !Task.isCancelled && !skipped else{return}
        withAnimation(.timingCurve(0.4,0,0.2,1,duration:reduceMotion ? 0 : 0.65)){gathered=true}
        try? await Task.sleep(nanoseconds:reduceMotion ? 100_000_000 : 900_000_000)
        guard !Task.isCancelled && !skipped else{return}
        finish()
    }
    func finish(){
        guard !exiting else{return}
        UserDefaults.standard.set(true,forKey:"hasSeenOpening")
        withAnimation(.timingCurve(0.4,0,0.2,1,duration:reduceMotion ? 0.15 : 0.7)){gathered=true;exiting=true}
        Task{
            try? await Task.sleep(nanoseconds:reduceMotion ? 150_000_000 : 700_000_000)
            withAnimation(.easeOut(duration:0.15)){model.startupVisible=false}
        }
    }
}
