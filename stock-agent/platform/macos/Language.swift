import Foundation

// Follow the Mac's first preferred language; unsupported languages use English.
// System/app language preferences are read at launch, with no separate in-app setting.
enum AppLanguage {
    static func isChinese(_ languages:[String])->Bool {
        guard let first=languages.first else{return false}
        return first.lowercased().split(separator:"-").first == "zh"
    }
    static var isChinese:Bool { isChinese(UserDefaults.standard.stringArray(forKey:"AppleLanguages") ?? Locale.preferredLanguages) }
    static var code:String { isChinese ? "zh" : "en" }
}
func L(_ chinese:String,_ english:String)->String { AppLanguage.isChinese ? chinese : english }
