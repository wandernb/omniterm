// OmniTerm's Notification Center widget: how many agent notifications need you,
// and the newest ones. It reads the snapshot the app writes to
// ~/Library/Application Support/OmniTerm/widget.json and opens the Inbox when
// clicked. Built by scripts/build-app.sh.

import SwiftUI
import WidgetKit

struct OmniWidgetItem: Decodable, Hashable {
    let title: String
    let severity: String
    let agent: String
    let updated: Double
}

struct OmniWidgetSnapshot: Decodable {
    let attentionCount: Int
    let unreadCount: Int
    let items: [OmniWidgetItem]

    enum CodingKeys: String, CodingKey {
        case attentionCount = "attention_count"
        case unreadCount = "unread_count"
        case items
    }

    static let empty = OmniWidgetSnapshot(attentionCount: 0, unreadCount: 0, items: [])

    init(attentionCount: Int, unreadCount: Int, items: [OmniWidgetItem]) {
        self.attentionCount = attentionCount
        self.unreadCount = unreadCount
        self.items = items
    }

    // The extension is sandboxed, so NSHomeDirectory() is its container. The
    // snapshot lives in the real home folder, readable through a read-only
    // exception for this one path.
    static func load() -> OmniWidgetSnapshot? {
        guard let home = getpwuid(getuid())?.pointee.pw_dir else { return nil }
        let path = String(cString: home) + "/Library/Application Support/OmniTerm/widget.json"
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        return try? JSONDecoder().decode(OmniWidgetSnapshot.self, from: data)
    }
}

struct OmniWidgetEntry: TimelineEntry {
    let date: Date
    let snapshot: OmniWidgetSnapshot?
}

struct OmniWidgetProvider: TimelineProvider {
    func placeholder(in context: Context) -> OmniWidgetEntry {
        OmniWidgetEntry(date: Date(), snapshot: OmniWidgetSnapshot(
            attentionCount: 2, unreadCount: 3,
            items: [OmniWidgetItem(title: "Deploy needs your approval", severity: "urgent", agent: "Nightly", updated: 0),
                    OmniWidgetItem(title: "Review finished", severity: "attention", agent: "Review", updated: 0)]))
    }

    func getSnapshot(in context: Context, completion: @escaping (OmniWidgetEntry) -> Void) {
        completion(context.isPreview ? placeholder(in: context) : OmniWidgetEntry(date: Date(), snapshot: OmniWidgetSnapshot.load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<OmniWidgetEntry>) -> Void) {
        // The app asks WidgetKit to reload whenever notifications change; the
        // refresh date only keeps relative times from going stale.
        let entry = OmniWidgetEntry(date: Date(), snapshot: OmniWidgetSnapshot.load())
        completion(Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(15 * 60))))
    }
}

private let peach = Color(red: 247 / 255, green: 177 / 255, blue: 133 / 255)
private let charcoal = Color(red: 0.13, green: 0.13, blue: 0.14)

private func marker(_ severity: String) -> String {
    switch severity {
    case "urgent": return "‼"
    case "attention": return "•"
    default: return "·"
    }
}

private func markerColor(_ severity: String) -> Color {
    switch severity {
    case "urgent": return .red
    case "attention": return peach
    default: return .secondary
    }
}

struct OmniWidgetHeader: View {
    var body: some View {
        HStack(spacing: 4) {
            Text("OMNI ·").font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(charcoal.opacity(0.7))
            Text("Inbox").font(.system(size: 11, weight: .bold)).foregroundStyle(charcoal)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(peach, in: RoundedRectangle(cornerRadius: 6))
    }
}

struct OmniWidgetCount: View {
    let snapshot: OmniWidgetSnapshot

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(snapshot.attentionCount)")
                .font(.system(size: 38, weight: .bold, design: .rounded))
                .foregroundStyle(snapshot.attentionCount > 0 ? Color.primary : Color.secondary)
            Text(snapshot.attentionCount == 1 ? "needs you" : "need you")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            if snapshot.unreadCount > snapshot.attentionCount {
                Text("\(snapshot.unreadCount) unread")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
        }
    }
}

struct OmniWidgetRow: View {
    let item: OmniWidgetItem

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(marker(item.severity)).font(.system(size: 12, weight: .bold)).foregroundStyle(markerColor(item.severity))
                .frame(width: 12, alignment: .center)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.title).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                Text(item.agent).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

struct OmniWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: OmniWidgetEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            OmniWidgetHeader()
            if let snapshot = entry.snapshot {
                if family == .systemSmall {
                    small(snapshot)
                } else {
                    medium(snapshot)
                }
            } else {
                Spacer(minLength: 0)
                Text("Open OmniTerm to see notifications")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
        }
        .widgetURL(URL(string: "omniterm://inbox"))
        .containerBackground(.background, for: .widget)
    }

    @ViewBuilder
    private func small(_ snapshot: OmniWidgetSnapshot) -> some View {
        OmniWidgetCount(snapshot: snapshot)
        Spacer(minLength: 0)
        if let first = snapshot.items.first {
            OmniWidgetRow(item: first)
        } else {
            Text("All caught up").font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func medium(_ snapshot: OmniWidgetSnapshot) -> some View {
        HStack(alignment: .top, spacing: 14) {
            OmniWidgetCount(snapshot: snapshot).frame(width: 78, alignment: .leading)
            VStack(alignment: .leading, spacing: 6) {
                if snapshot.items.isEmpty {
                    Text("All caught up").font(.system(size: 12)).foregroundStyle(.secondary)
                } else {
                    ForEach(snapshot.items.prefix(3), id: \.self) { item in
                        OmniWidgetRow(item: item)
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }
}

@main
struct OmniInboxWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "OmniInbox", provider: OmniWidgetProvider()) { entry in
            OmniWidgetView(entry: entry)
        }
        .configurationDisplayName("OmniTerm Inbox")
        .description("Agent notifications that need you.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
