import Foundation
import Security
import LocalAuthentication

struct TelegramCredentials:Codable {
    let token:String
    let chatID:String
}
enum TelegramFailure:Error,LocalizedError {
    case invalid,credentials,chat,network,storage
    var errorDescription:String? {
        switch self {
        case .invalid:return L("请填写有效的 Bot Token 和数字 Chat ID（群组通常为负数）", "Enter a valid Bot Token and numeric Chat ID (usually negative for groups).")
        case .credentials:return L("Bot Token 未通过验证，请检查或在 BotFather 中更新", "Bot Token verification failed. Check it or update it in BotFather.")
        case .chat:return L("目标会话不可访问，请先给机器人发送 /start，或把机器人加入目标群组，并检查 Chat ID", "Chat unavailable. Send /start to the bot or add it to the target group, then check Chat ID.")
        case .network:return L("无法连接 Telegram，请检查网络后重试；已有配置没有被替换", "Cannot connect to Telegram. Check your connection and retry; saved settings have not been replaced.")
        case .storage:return L("未能访问本机钥匙串，已有配置没有被替换", "Cannot access this Mac’s Keychain; saved settings have not been replaced.")
        }
    }
}
struct TelegramStore {
    let mode:String
    var service:String { "ai.averra.desktop.\(mode).telegram" }
    var query:[String:Any] {[kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:service,kSecAttrAccount as String:"connection"]}
    var configured:Bool {
        let context=LAContext();context.interactionNotAllowed=true
        var q=query;q[kSecReturnAttributes as String]=true;q[kSecUseAuthenticationContext as String]=context
        let status=SecItemCopyMatching(q as CFDictionary,nil)
        return status==errSecSuccess || status==errSecInteractionNotAllowed
    }
    func read() throws ->TelegramCredentials {
        var q=query;q[kSecReturnData as String]=true
        var result:CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary,&result)==errSecSuccess,let bytes=result as? Data else{throw TelegramFailure.storage}
        return try JSONDecoder().decode(TelegramCredentials.self,from:bytes)
    }
    func save(_ credentials:TelegramCredentials) throws {
        let bytes=try JSONEncoder().encode(credentials)
        let status=SecItemUpdate(query as CFDictionary,[kSecValueData as String:bytes] as CFDictionary)
        if status==errSecItemNotFound {
            var q=query;q[kSecValueData as String]=bytes;q[kSecAttrAccessible as String]=kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            guard SecItemAdd(q as CFDictionary,nil)==errSecSuccess else{throw TelegramFailure.storage}
        }else if status != errSecSuccess{throw TelegramFailure.storage}
    }
}
// Tokens appear in Telegram's API path; refuse redirects and never display URLSession errors.
final class TelegramNoRedirect:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
}
struct TelegramClient {
    static func validate(_ credentials:TelegramCredentials)throws {
        guard credentials.token.range(of:"^[0-9]+:[A-Za-z0-9_-]{20,}$",options:.regularExpression) != nil,
              credentials.chatID.range(of:"^-?[0-9]+$",options:.regularExpression) != nil else{throw TelegramFailure.invalid}
    }
    static func check(_ credentials:TelegramCredentials,session:URLSession) async throws ->String {
        try validate(credentials)
        let bot=try await call("getMe",credentials:credentials,session:session)
        guard bot["is_bot"] as? Bool == true,let username=bot["username"] as? String,
              username.range(of:"^[A-Za-z0-9_]+$",options:.regularExpression) != nil else{throw TelegramFailure.credentials}
        _=try await call("getChat",credentials:credentials,session:session)
        return username
    }
    static func send(_ text:String,credentials:TelegramCredentials,session:URLSession) async throws {
        try validate(credentials)
        let url=URL(string:"https://api.telegram.org/bot\(credentials.token)/sendMessage")!
        var request=URLRequest(url:url);request.httpMethod="POST";request.setValue("application/json",forHTTPHeaderField:"Content-Type")
        request.httpBody=try JSONSerialization.data(withJSONObject:["chat_id":credentials.chatID,"text":text])
        let data:Data;let response:URLResponse
        do {(data,response)=try await session.data(for:request)}catch{throw TelegramFailure.network}
        guard (response as? HTTPURLResponse)?.statusCode==200,
              let body=try? JSONSerialization.jsonObject(with:data) as? [String:Any],body["ok"] as? Bool==true,
              let result=body["result"] as? [String:Any],result["message_id"] as? Int != nil else{throw TelegramFailure.network}
    }
    private static func call(_ method:String,credentials:TelegramCredentials,session:URLSession) async throws ->[String:Any] {
        let url=URL(string:"https://api.telegram.org/bot\(credentials.token)/\(method)")!
        var request=URLRequest(url:url);request.httpMethod="POST";request.setValue("application/json",forHTTPHeaderField:"Content-Type")
        request.httpBody=try JSONSerialization.data(withJSONObject:method=="getChat" ? ["chat_id":credentials.chatID] : [:])
        let responseData:Data;let response:URLResponse
        do{(responseData,response)=try await session.data(for:request)}catch{throw TelegramFailure.network}
        let code=(response as? HTTPURLResponse)?.statusCode ?? 0
        if code==401 || code==404{throw TelegramFailure.credentials}
        guard code==200,let body=try? JSONSerialization.jsonObject(with:responseData) as? [String:Any],body["ok"] as? Bool == true,let result=body["result"] as? [String:Any] else{throw method=="getChat" ? TelegramFailure.chat : TelegramFailure.network}
        return result
    }
}
