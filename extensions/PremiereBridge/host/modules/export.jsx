/**
 * Project creation and sequence export: the two ends of a finished edit.
 *
 * Everything else in this host mutates a timeline that somebody else made and
 * leaves the result on screen. These two operations are what turn that into a
 * job with a start and a finish -- `project.new` gives an unattended run a
 * project to work in, and `sequence.export` turns the sequence it built into a
 * file on disk.
 *
 * Export strategy, in order, because no single call works on every build:
 *
 *   1. `sequence.exportAsMediaDirect(path, preset, workArea)` -- synchronous,
 *      renders inside Premiere, needs no other application. Preferred: when it
 *      returns, the file is finished, so the caller does not have to poll.
 *   2. `app.encoder.encodeSequence(...)` -- hands the job to Adobe Media
 *      Encoder. Asynchronous, so this reports `pending` and the Python side
 *      waits for the file rather than pretending it is done.
 *
 * A preset is not optional in either call, and Premiere will not invent one, so
 * `presets()` finds the installed .epr files and `resolvePreset` picks a
 * sensible H.264 match-source default. Guessing a path would fail at render
 * time with an error that says nothing useful.
 */

var NovaExport = (function () {
    var U = NovaUtil;

    /** Work area constants for exportAsMediaDirect. */
    var ENTIRE = 0;      // ENCODE_ENTIRE
    var IN_TO_OUT = 1;   // ENCODE_IN_TO_OUT
    var WORK_AREA = 2;   // ENCODE_WORK_AREA

    function workAreaOf(name) {
        if (name === 'in_to_out') { return IN_TO_OUT; }
        if (name === 'work_area') { return WORK_AREA; }
        return ENTIRE;
    }

    // ------------------------------------------------------------------
    // Projects
    // ------------------------------------------------------------------

    /**
     * Create a project at `path`, replacing any project already open.
     *
     * `app.newProject` returns a boolean on some builds and nothing on others,
     * so success is confirmed by looking at what is actually open afterwards
     * rather than by trusting the return value.
     */
    function newProject(params) {
        var target = params && params.path;
        if (!target) { throw U.fail('project.new needs a path'); }
        target = String(target);

        var folder = new File(target).parent;
        if (folder && !folder.exists) { folder.create(); }

        if (params.overwrite && new File(target).exists) {
            new File(target).remove();
        }

        // Close the current project *without prompting* first.
        //
        // This is the difference between an unattended run and a hang. If a
        // project with unsaved changes is open, `newProject` raises a modal
        // "save changes?" dialog inside Premiere. ExtendScript is
        // single-threaded, so that dialog blocks every subsequent call --
        // including the health check -- and the whole bridge looks dead from
        // Python with no indication that a human simply needs to click a
        // button. `closeDocument(saveFirst, promptIfDirty)` with both false
        // is the only call that cannot do that.
        try {
            if (app.project && typeof app.project.closeDocument === 'function') {
                app.project.closeDocument(0, 0);
            }
        } catch (closeError) { /* nothing open, or the build has no such call */ }

        try {
            app.newProject(target);
        } catch (e) {
            throw U.fail('Could not create a project at ' + target + ': ' + e, {
                hint: 'Check the folder exists and is writable, and that no '
                    + 'project is open with unsaved changes blocking the switch.'
            });
        }

        if (!app.project) {
            throw U.fail('Premiere reported no open project after project.new');
        }
        return {
            created: String(app.project.path || target),
            name: String(app.project.name)
        };
    }

    // ------------------------------------------------------------------
    // Export presets
    // ------------------------------------------------------------------

    /** Folders Premiere keeps .epr presets in, most specific first. */
    function presetRoots() {
        var roots = [];
        function add(candidate) {
            if (!candidate) { return; }
            var folder = new Folder(candidate);
            if (folder.exists) { roots[roots.length] = folder; }
        }
        try {
            // User presets win: somebody who made one meant it.
            add(Folder.userData.fsName + '/Adobe/Adobe Media Encoder/'
                + '/Presets');
        } catch (e) { /* no user presets */ }
        try {
            var appFolder = new File(app.path || '').fsName;
            add(appFolder + '/MediaIO/systempresets');
        } catch (e2) { /* fall through */ }
        return roots;
    }

    function decodeName(value) {
        try { return decodeURIComponent(value); }
        catch (e) { return value; }
    }

    function collect(folder, out, depth) {
        if (depth > 4 || out.length > 800) { return; }
        var entries = folder.getFiles();
        for (var i = 0; i < entries.length; i++) {
            var entry = entries[i];
            if (entry instanceof Folder) {
                collect(entry, out, depth + 1);
            } else {
                // File.name is URI-encoded ("HD%201080p"), which turns every
                // name match into a silent miss. Decode once, here, so nothing
                // downstream has to know.
                var label = decodeName(String(entry.name));
                if (label.toLowerCase().indexOf('.epr') > 0) {
                    out[out.length] = { name: label,
                                        path: String(entry.fsName) };
                }
            }
        }
    }

    /** Every installed export preset, optionally filtered by substring. */
    function presets(params) {
        var filter = String((params && params.match) || '').toLowerCase();
        var roots = presetRoots();
        var found = [];
        for (var i = 0; i < roots.length; i++) {
            collect(roots[i], found, 0);
        }
        if (!filter) { return { presets: found, count: found.length }; }
        var kept = [];
        for (var j = 0; j < found.length; j++) {
            if (found[j].name.toLowerCase().indexOf(filter) >= 0) {
                kept[kept.length] = found[j];
            }
        }
        return { presets: kept, count: kept.length, match: filter };
    }

    /**
     * The preset to render with.
     *
     * An explicit path wins. Otherwise the first installed preset whose name
     * matches one of the preferences below, in order -- match-source H.264
     * first, because it keeps the sequence's own resolution and frame rate and
     * therefore never silently reframes the edit.
     */
    var PREFERRED = [
        'h264 match source - high bitrate',
        'match source - high bitrate',
        'h264 match source',
        'match source - adaptive high bitrate',
        'youtube 1080p full hd'
    ];

    function resolvePreset(explicit) {
        if (explicit) {
            var given = new File(String(explicit));
            if (!given.exists) {
                throw U.fail('Export preset not found: ' + explicit, {
                    hint: 'caps.presets lists the .epr files installed here'
                });
            }
            return String(given.fsName);
        }
        var all = presets({}).presets;
        for (var p = 0; p < PREFERRED.length; p++) {
            for (var i = 0; i < all.length; i++) {
                if (all[i].name.toLowerCase().indexOf(PREFERRED[p]) === 0) {
                    return all[i].path;
                }
            }
        }
        // Last resort: any H.264 preset at all.
        for (var j = 0; j < all.length; j++) {
            if (all[j].name.toLowerCase().indexOf('h264') >= 0) {
                return all[j].path;
            }
        }
        throw U.fail('No export preset could be found on this machine', {
            hint: 'Pass "preset" with the path to an .epr file. '
                + 'caps.presets lists what is installed.'
        });
    }

    // ------------------------------------------------------------------
    // Export
    // ------------------------------------------------------------------

    /** Windows "C:\..." / UNC "\\host\share" / POSIX "/..." . */
    function isAbsolute(path) {
        var text = String(path || '');
        if (text.length > 1 && text.charAt(1) === ':') { return true; }
        if (text.indexOf('//') === 0 || text.indexOf('\\') === 0) { return true; }
        return text.charAt(0) === '/';
    }

    function sequenceNamed(wanted) {
        if (!wanted) { return U.activeSequence(); }
        var project = app.project;
        for (var i = 0; i < project.sequences.numSequences; i++) {
            if (String(project.sequences[i].name) === String(wanted)) {
                return project.sequences[i];
            }
        }
        throw U.fail('No sequence named "' + wanted + '"', {
            hint: 'sequence.list shows what exists'
        });
    }

    /**
     * Render a sequence to a file.
     *
     * Returns `{ started: true, complete: bool, path, method }`. `complete` is
     * the honest part: the direct export blocks until the file exists, the
     * Media Encoder route does not, and the caller needs to know which one it
     * got rather than assuming the file is ready.
     */
    function exportSequence(params) {
        params = params || {};
        var sequence = sequenceNamed(params.sequence);
        var path = String(params.path || '');
        if (!path) { throw U.fail('sequence.export needs a path'); }
        if (!isAbsolute(path)) {
            // Premiere has its own working directory, so a relative path here
            // resolves somewhere nobody intended. The direct export then fails
            // for a reason that reads as a codec problem, the call falls
            // through to Media Encoder, and the result is a render that says
            // it started and never writes a file. Refusing is far kinder.
            throw U.fail('sequence.export needs an absolute path, got "'
                         + path + '"', {
                code: 'validation_error',
                hint: 'Premiere resolves a relative path against its own '
                    + 'working directory, not the working directory of '
                    + 'whatever sent the request.'
            });
        }

        var folder = new File(path).parent;
        if (folder && !folder.exists) { folder.create(); }
        if (new File(path).exists && params.overwrite !== false) {
            new File(path).remove();
        }

        var preset = resolvePreset(params.preset);
        var range = workAreaOf(String(params.range || 'entire'));
        var errors = [];

        // 1. Direct, synchronous export.
        if (typeof sequence.exportAsMediaDirect === 'function') {
            try {
                sequence.exportAsMediaDirect(path, preset, range);
                if (new File(path).exists) {
                    return {
                        started: true, complete: true, method: 'direct',
                        path: path, preset: preset,
                        sequence: String(sequence.name),
                        size: Number(new File(path).length || 0)
                    };
                }
                errors[errors.length] =
                    'exportAsMediaDirect returned without writing a file';
            } catch (e) {
                errors[errors.length] = 'exportAsMediaDirect: '
                    + String(e.message || e);
            }
        }

        // 2. Adobe Media Encoder, asynchronous.
        try {
            if (app.encoder && typeof app.encoder.encodeSequence === 'function') {
                app.encoder.launchEncoder();
                app.encoder.encodeSequence(sequence, path, preset, range, 1);
                if (typeof app.encoder.startBatch === 'function') {
                    app.encoder.startBatch();
                }
                return {
                    started: true, complete: false, method: 'media_encoder',
                    path: path, preset: preset,
                    sequence: String(sequence.name),
                    note: 'Adobe Media Encoder is rendering. The file appears '
                        + 'when it finishes.'
                };
            }
        } catch (e2) {
            errors[errors.length] = 'encodeSequence: ' + String(e2.message || e2);
        }

        throw U.fail('Could not export the sequence', {
            hint: 'Both the direct export and the Media Encoder route failed. '
                + 'The preset may not match this sequence.',
            detail: { attempts: errors, preset: preset, path: path }
        });
    }

    return {
        newProject: newProject,
        presets: presets,
        resolvePreset: resolvePreset,
        exportSequence: exportSequence
    };
}());
