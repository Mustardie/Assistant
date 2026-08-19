/**
 * Read Premiere's Speech to Text / Transcript data.
 *
 * Premiere's transcription lives in the Text panel (Transcript tab), and newer
 * builds surface the same data as Text-Based Editing. Adobe exposes NO
 * documented ExtendScript API for reading it. That is a fact about the host,
 * not a gap in this bridge, so this module does exactly two honest things:
 *
 *   transcript.caps  reports which of the possible access routes actually
 *                    exist on THIS build, measured rather than assumed.
 *   transcript.read  tries those routes in order and returns raw marker data,
 *                    or fails with `unsupported` naming the manual export.
 *
 * The one route that genuinely works on shipping builds is XMP. Premiere's
 * speech analysis writes its result into the project item's XMP as a Speech
 * track of word markers (xmpDM:Tracks -> trackType "Speech"), which
 * `getXMPMetadata()` returns as a string. Word markers are returned here
 * unmerged, with their tick scale, and grouped into lines on the Python side
 * where the logic is testable.
 *
 * Nothing here invents timings. If no transcript is present, `found` is false
 * and `checked` says where we looked -- the caller then asks the user to export
 * from the Transcript panel and imports that file instead.
 */

var NovaTranscript = (function () {
    var U = NovaUtil;

    function safe(fn, fallback) {
        try { return fn(); } catch (e) { return fallback; }
    }

    // ------------------------------------------------------------------
    // Capability probe
    // ------------------------------------------------------------------

    function caps() {
        var sequence = safe(function () { return app.project.activeSequence; }, null);
        var item = safe(function () { return app.project.rootItem.children[0]; }, null);

        var report = {
            version: String(app.version),
            apis: {
                // The XMP route: present on every build we have seen, and the
                // only one that has ever returned real transcript data.
                getXMPMetadata: !!(item && U.has(item, 'getXMPMetadata')),
                getProjectMetadata: !!(item && U.has(item, 'getProjectMetadata')),
                // Caption-track routes. These write captions on every build;
                // whether they can be READ back differs, so both are probed.
                createCaptionTrack: !!(sequence && U.has(sequence, 'createCaptionTrack')),
                getCaptionTracks: !!(sequence && U.has(sequence, 'getCaptionTracks')),
                captionTracks: !!(sequence && sequence.captionTracks !== undefined),
                // Never documented; probed so a future build that adds it is
                // discovered rather than missed.
                getTranscript: !!(sequence && U.has(sequence, 'getTranscript')),
                projectItemTranscript: !!(item && U.has(item, 'getTranscript'))
            }
        };

        report.readable = report.apis.getXMPMetadata
            || report.apis.getCaptionTracks
            || report.apis.captionTracks
            || report.apis.getTranscript;

        report.note = report.readable
            ? 'Transcript data may be readable on this build; transcript.read '
                + 'will report per-asset whether any is actually present.'
            : 'This Premiere build exposes no scriptable route to transcript '
                + 'data. Export it from the Text panel (Transcript tab) and '
                + 'import the file instead.';
        report.manual_export = 'Text panel > Transcript tab > ... menu > '
            + 'Export > Export transcript (.txt) or Export captions (.srt)';
        return report;
    }

    // ------------------------------------------------------------------
    // Reading
    // ------------------------------------------------------------------

    function read(params) {
        params = params || {};
        var checked = [];

        var item = null;
        if (params.asset) {
            item = NovaProject.findAsset(params.asset);
        } else {
            throw U.fail('transcript.read needs an "asset" to read from', {
                hint: 'Pass the media path or project item name'
            });
        }

        // -- route 1: XMP speech track ---------------------------------
        checked[checked.length] = 'xmp_speech_track';
        var xmp = safe(function () { return String(item.getXMPMetadata()); }, '');
        if (xmp) {
            var speech = parseSpeechTrack(xmp);
            if (speech && speech.markers.length) {
                return {
                    found: true,
                    method: 'xmp_speech_track',
                    asset: String(item.name),
                    path: safe(function () { return String(item.getMediaPath()); }, ''),
                    scale: speech.scale,
                    markers: speech.markers,
                    checked: checked,
                    note: 'Word-level markers from Premiere speech analysis, '
                        + 'stored in the item XMP.'
                };
            }
        }

        // -- route 2: a scriptable caption/transcript API, if this build has one
        var sequence = safe(function () { return app.project.activeSequence; }, null);
        if (sequence && U.has(sequence, 'getCaptionTracks')) {
            checked[checked.length] = 'sequence_caption_tracks';
            var fromCaptions = readCaptionTracks(sequence);
            if (fromCaptions.length) {
                return {
                    found: true,
                    method: 'sequence_caption_tracks',
                    asset: String(item.name),
                    scale: 1,
                    markers: fromCaptions,
                    checked: checked,
                    note: 'Read from caption tracks on the active sequence.'
                };
            }
        }

        return {
            found: false,
            method: '',
            asset: String(item.name),
            path: safe(function () { return String(item.getMediaPath()); }, ''),
            markers: [],
            checked: checked,
            note: 'No transcript data is reachable for this item by script. If '
                + 'you have transcribed it in the Text panel, export the '
                + 'transcript and import the file.',
            manual_export: 'Text panel > Transcript tab > ... menu > Export'
        };
    }

    /**
     * Pull the Speech track out of an XMP document.
     *
     * Parsed with regular expressions rather than E4X: the XMP Premiere writes
     * mixes several namespaces and nests rdf:Bag/rdf:Seq inconsistently between
     * versions, and a tolerant scan for the marker fields survives that where a
     * strict XML path does not.
     */
    function parseSpeechTrack(xmp) {
        var tracks = xmp.match(/<xmpDM:Tracks>[\s\S]*?<\/xmpDM:Tracks>/);
        if (!tracks) { return null; }
        var block = tracks[0];

        // Only a Speech/transcript track is of interest -- a Comment or Chapter
        // marker track would otherwise be mistaken for dialogue.
        if (!/trackType\s*[=>]\s*"?\s*(Speech|Transcript)/i.test(block)) {
            return null;
        }

        var scale = 254016000000;   // Premiere's tick scale, the usual value
        var rate = block.match(/<xmpDM:frameRate>\s*f?(\d+)\s*<\/xmpDM:frameRate>/i)
            || block.match(/xmpDM:frameRate\s*=\s*"f?(\d+)"/i);
        if (rate) {
            var parsed = Number(rate[1]);
            if (parsed > 0) { scale = parsed; }
        }

        var markers = [];
        var items = block.match(/<rdf:li[\s\S]*?<\/rdf:li>/g) || [];
        for (var i = 0; i < items.length; i++) {
            var marker = parseMarker(items[i]);
            if (marker) { markers[markers.length] = marker; }
        }
        return { scale: scale, markers: markers };
    }

    function parseMarker(chunk) {
        var start = field(chunk, 'startTime');
        if (start === null) { return null; }
        var text = textField(chunk, 'name');
        if (!text) { text = textField(chunk, 'comment'); }
        if (!text) { return null; }

        var duration = field(chunk, 'duration');
        var probability = field(chunk, 'probability');
        return {
            start: start,
            duration: duration === null ? 0 : duration,
            text: text,
            speaker: textField(chunk, 'speaker') || '',
            probability: probability === null ? 1 : probability
        };
    }

    function field(chunk, name) {
        var element = chunk.match(
            new RegExp('<xmpDM:' + name + '>\\s*f?(-?[0-9.]+)\\s*</xmpDM:' + name + '>', 'i'));
        if (element) { return Number(element[1]); }
        var attribute = chunk.match(
            new RegExp('xmpDM:' + name + '\\s*=\\s*"f?(-?[0-9.]+)"', 'i'));
        return attribute ? Number(attribute[1]) : null;
    }

    function textField(chunk, name) {
        var element = chunk.match(
            new RegExp('<xmpDM:' + name + '>([\\s\\S]*?)</xmpDM:' + name + '>', 'i'));
        var raw = element ? element[1] : null;
        if (raw === null) {
            var attribute = chunk.match(
                new RegExp('xmpDM:' + name + '\\s*=\\s*"([^"]*)"', 'i'));
            raw = attribute ? attribute[1] : null;
        }
        if (raw === null) { return ''; }
        return decode(raw.replace(/<[^>]*>/g, '')).replace(/^\s+|\s+$/g, '');
    }

    function decode(text) {
        return String(text)
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
            .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
            .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
    }

    /** Read caption clips from a build that exposes caption tracks. */
    function readCaptionTracks(sequence) {
        var out = [];
        var tracks = safe(function () { return sequence.getCaptionTracks(); }, null);
        if (!tracks) { return out; }
        var count = safe(function () { return Number(tracks.numTracks); }, 0);
        for (var t = 0; t < count; t++) {
            var track = safe(function () { return tracks[t]; }, null);
            if (!track) { continue; }
            var clips = safe(function () { return track.clips; }, null);
            var clipCount = safe(function () { return Number(clips.numItems); }, 0);
            for (var c = 0; c < clipCount; c++) {
                var clip = clips[c];
                var text = safe(function () { return String(clip.text || clip.name); }, '');
                if (!text) { continue; }
                out[out.length] = {
                    start: U.secondsOf(clip.start),
                    duration: U.secondsOf(clip.end) - U.secondsOf(clip.start),
                    text: text,
                    speaker: '',
                    probability: 1
                };
            }
        }
        return out;
    }

    return {
        caps: caps,
        read: read
    };
}());
