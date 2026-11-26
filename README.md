# WombatGC

A desktop app for analytical data, for teaching and research. The start screen asks what kind of data you have, or you can just open a file and let the app work out what it is (or drag the file onto the window).

| Technique | Opens |
|---|---|
| **GC-MS · GC-FID** | Agilent `.D` (ChemStation, MassHunter, FID/TCD `.ch`), ANDI netCDF |
| **GCxGC** | ChromSpace / ChromaTOF netCDF exports |
| **NMR** | Bruker experiment folders or `.zip`, TopSpin ASCII exports, JCAMP-DX |
| **FT-IR** | Bruker OPUS (`.0`, `.1` …), JCAMP-DX, CSV |
| **LC-MS** | Thermo `.raw`, mzML, mzXML |
| **CSV / text** | any columns of numbers: you choose the columns and the kind of data |
| **Compare samples** | up to 5 chromatograms (GC, GCxGC or LC-MS) side by side, with their mass spectra |

Every technique has publication-quality image export (PNG/TIFF at any dpi, SVG/PDF) and tables that export to Excel or paste straight into Word (see *Exporting tabulated data*). The **figure maker** puts plots, structures, labels and arrows together on one page, and the **report builder** turns figures, tables and report text into a student or client report (PDF or Word). See *Figures and reports*.

## Installing it (students and staff)

Download the latest installer from the **[Releases](https://github.com/mwilde5/wombat-gc/releases/latest)** page of this repository (under *Assets*):

- **Windows:** `WombatGC-…-Windows-Setup.exe`. Run it; no administrator rights are needed. If Windows shows *"Windows protected your PC"*, click **More info → Run anyway** (the installer isn't code-signed yet).
- **Mac (Apple Silicon, M1 or later):** `WombatGC-…-macOS-AppleSilicon.dmg`. **Older Intel Macs:** `…-macOS-Intel.dmg`. Open it and drag WombatGC into Applications. The first time, macOS will say it can't check the app: go to **System Settings → Privacy & Security**, scroll down and click **Open Anyway**.

Everything is included; you don't need Python. To check which Mac you have: Apple menu → About This Mac → *Chip* (Apple M…) or *Processor* (Intel).

## Running from the source code


**Windows:** double-click **`Start WombatGC.bat`**. The first time you run it, it sets up its own Python environment inside this folder, which takes a minute or two and needs internet access. After that it starts straight away. You can also drag a `.D` folder, Bruker zip or other data file onto the .bat file to open that run directly.

**Mac / Linux:** double-click `start_wombatgc.command` (or run it from Terminal). The first run sets up Python packages the same way. On a Mac, install Python from python.org first (the `python3` that comes with macOS is too old). The first time, macOS may refuse to open the file because it was downloaded: right-click it and choose **Open**, or allow it in **System Settings ▸ Privacy & Security**. NIST MS Search is Windows-only; everything else works on both.

**Manual setup:** `pip install -r requirements.txt`, then `python wombat.py [file]`.

You need Python 3.10–3.13 (Windows or Mac). If it isn't installed, install it from python.org or the university Software Center, and tick *Add python.exe to PATH* during installation.

## Chromatography

It reads:

- Agilent `.D` folders: **ChemStation** (`DATA.MS`), **MassHunter** (`AcqData/MSPeak.bin`), and **detector signals** such as `FID1A.ch` from ChemStation or OpenLab CDS
- **ANDI netCDF** (`.cdf`) exports from any vendor, including ChromSpace (Markes BenchTOF) and ChromaTOF GCxGC data

You can:

- view the TIC or extracted ion chromatograms (EICs)
- click anywhere to see the mass spectrum at that point
- export chromatograms, spectra and peak tables as CSV
- export chromatograms and spectra as high-resolution images (PNG/TIFF at any dpi, or SVG/PDF vector)
- integrate peaks manually
- view GCxGC data as a smooth 2D colour plot with a synced, rotatable 3D view, integrate 2D peaks (blobs), and export the 2D matrix, publication figures and **3D models** (GLB, FBX, STL)
- identify spectra by library search, against free open libraries (MassBank, MoNA, or your own MSP files) or by sending them to **NIST MS Search** if it's installed on the PC

### Using it

| To do this | Do this |
|---|---|
| Open a run | **File ▸ Open**, or drag a `.D` folder onto the window. Recently opened runs are under **File ▸ Open recent**. |
| See a spectrum | In **Spectrum** mode (key `1`), click the chromatogram. Drag across a region to average the spectra in it. Use ← / → to step one scan at a time. |
| Background-subtract | In **Background** mode (key `3`), click or drag across a quiet region. *Subtract bkg* is then ticked automatically. |
| Show an EIC | Type an m/z value in the toolbar box and press Enter. To sum several ions, separate them with commas (`145, 176`). The ± box sets the extraction window. Shortcut: **double-click a peak in the spectrum**. |
| Integrate | In **Integrate** mode (key `2`), drag from the start to the end of a peak. Ctrl+Z undoes the last peak. To delete a peak, select its row and press Delete. Double-click the *Name / note* cell to label a peak. |
| Zoom | Mouse wheel zooms the time axis (the y-axis rescales itself). Ctrl+wheel zooms intensity. Right-drag draws a zoom box. Middle-drag pans. Right-click steps back out. **Home** returns to the full view. |
| Export | Use the **CSV…** / **Image…** buttons above each plot, or the **File** menu. The image dialog sets format, dpi, size in mm, font size, and whether to use the current zoom or the full range. |
| Move peak labels | Drag a peak label away from the trace; a thin line joins it to its peak. **Labels ▸ Auto-arrange labels** spaces out crowded labels for the current zoom, and **Labels** also sets the label text (peak number, retention time, name, or number and name). Image exports keep your layout. |
| Add or remove m/z labels | Turn on **Edit labels** above the spectrum and click a peak to add or remove its label; drag labels to move them. The arrow beside it sets how many peaks are labelled automatically. Image exports keep your changes. |
| Copy | **Copy** above the spectrum puts tab-separated m/z/abundance on the clipboard, ready to paste into Excel or NIST MS Search. **Copy** in the Peaks panel does the same for the peak table. |

### Library search (Library search tab)

There are three ways to identify a spectrum:

1. **Open libraries, searched inside the app** (**Search libraries**, or F5)
   - The first time you click it, the app offers to download **MassBank**: free, about 13,000 EI spectra, roughly 120 MB. It fetches the newest release that includes an MSP file.
   - You can add the **MoNA GC-MS** library or **your own in-house libraries** via **Libraries… ▸ Add MSP file…**. *Get MoNA GC-MS…* opens the MoNA downloads page so you can download the MSP file first. Any NIST-format `.msp` file works.
   - Each library is indexed once, taking a few seconds per 100k spectra. After that, it loads and searches in well under a second.
   - Tick **Auto** to re-search every time you click a new point on the chromatogram.
   - The selected hit is drawn upside-down under your spectrum (a head-to-tail plot). **Image…** can export this head-to-tail plot.
   - **Match** is a weighted dot product (Stein & Scott, 1994) on a 0–999 scale. **R.Match** ignores peaks that are absent from the library spectrum, which is useful when another compound co-elutes.
2. **NIST MS Search** (**Search NIST**, or F6; Windows only)
   - This sends the current spectrum to the NIST program installed alongside ChemStation or MassHunter. The app normally finds the NIST folder (`C:\NIST*\MSSEARCH`) automatically; if not, set it under **Libraries…**.
   - **One-time setting in NIST** so hits come back into this app as well as the NIST window: *Options ▸ Library Search Options ▸ Automation* tab ▸ tick **Automatic Search On**, and set *Number of hits to print*, e.g. 20.
   - ChemStation and MassHunter use the same hand-off file (`AUTOIMP.MSD`). The app backs it up and restores it after each search.
3. **Web links** for the selected hit:
   - **NIST WebBook**: compound page, found by CAS number or InChI
   - **PubChem**
   - **Library record**: the original MassBank or MoNA entry

   Double-click a hit to open the WebBook. Right-click a hit to copy the hit list.

Coverage of the free libraries is far smaller than NIST's (tens of thousands of EI spectra versus about 350,000), so a poor open-library match doesn't mean the compound is unknown.

### GC-FID (and other single-channel detectors)

Open the `.D` folder as usual. If it holds several signals (e.g. `FID1A` and `FID2B`, or MS data plus an FID), pick one in the **Data** box.

- **What works:** viewing, zooming, CSV and image export, and manual integration. Areas are in pA·s.
- **What's hidden or greyed out:** the spectrum panel, EICs and library search, since an FID has no spectra.
- **GCxGC-FID:** set a modulation period and the 2D and 3D views work the same way.

### GCxGC (comprehensive 2D GC)

Open a netCDF export with **Open .cdf…** (Ctrl+Shift+O) or by dragging it onto the window.

- **Modulation period.** The app reads it from the file or method name (e.g. `…_4s_…`) and checks that the signal really repeats at that interval. If there's no hint in the name, it detects the period from the signal alone and says so. You can change it, plus a **phase shift** to move wrapped-around peaks back into place, in the second toolbar row; press Enter to apply. Your settings are remembered per file. Setting the period to 0 treats the file as ordinary 1D GC-MS.
- **Views.** **2D** shows the colour plot; **1D raw** shows the modulated 1D chromatogram (Ctrl+2 / Ctrl+1).
- **Colours.**
  - Choose a scheme. Three GCxGC schemes put **zero signal at white**.
    - **Mike's preference** (the default): white → blue → navy → teal → green → yellow → red.
    - **GCxGC: white → blue → orange**: pale to dark blue, with orange/yellow on the strongest peaks, like classic contour figures.
    - **Banded 3D: white → navy → rainbow bands**: the bands climb tall peaks like contour lines in 3D.
  - Ten more (Viridis, Jet, Hot, Electric, Portland, Plasma, Inferno, Turbo, Greys and the diverging **Blue → Red**: dark blue → white → dark red) are exactly those of the "single trace → GC×GC 3D" page.
  - **Auto levels** puts the noise floor (median) at the bottom of the scale and saturates at the 99.8th percentile of the area in view. Moderate peaks therefore get real colour instead of being washed out by the single largest peak.
  - **Edit…** opens a colour-stop editor: add, move, recolour, remove or reverse stops. Your own scheme is saved as *Custom*.
  - Choose a scale: linear, square root or log.
  - The handles on the colour bar set the noise floor and saturation. **Auto levels** fits the colour range to the area in view.
- **Smooth surface.**
  - The plot is drawn with cubic interpolation between modulations, so peaks show as smooth blobs rather than 4-second-wide stripes. You can turn this off under **Edit…**.
  - Also under **Edit…** is an optional **display baseline**: a rolling-minimum baseline (e.g. 20 s), as on the single-trace page.
  - Both affect the plot, the 3D view and 3D models only. Integration always uses the raw data.
- **3D view.**
  - A rotatable 3D surface sits beside the 2D plot. It shows whatever region the 2D plot is zoomed to, with the same colours, scaling and colour range. A red pin marks the selected point. Untick *Follow 2D view* to show the whole run.
  - Controls: left-drag rotates, the wheel zooms, and middle-drag (or Ctrl + left-drag) pans. There's also a height slider and a choice of normal or high detail.
  - Colour mode: **Colours as 2D** uses the same colour range as the plot, so tall peaks saturate. **Colour by height** runs the scheme from floor to tallest peak, which gives the banded look with the Banded 3D scheme. The background is dark grey by default so a white floor stands out; change it under **Edit…**.
  - **Pop out** opens it in its own window, e.g. on a second monitor. **Dock** or closing that window puts it back. **View ▸ 3D view** (Ctrl+3) shows or hides it, and **Save view…** saves a PNG.
  - It needs OpenGL. If that's unavailable, e.g. over Remote Desktop, the panel says so and everything else still works. Setting the environment variable `QT_OPENGL=software` can help there.
- **3D models (3D model…).** These are solid models with a vertex-coloured surface, walls and a base, built the same way as the single-trace page's models.
  - **GLB:** PowerPoint, Windows 3D Viewer, the web, Blender, Unity.
  - **FBX:** Blender, Unity, Unreal.
  - **STL:** for 3D printing. It has no colours, is in millimetres with Z up, and is watertight.
  - **Options:** current view or whole run, model width, stretched (2.4 : 1) or square aspect, height exaggeration, and resolution along each axis. Where there are fewer modulations than the resolution, the surface is smoothly interpolated up; where there are more, it's max-pooled so peak tops survive. You can also make colours follow the on-screen colour range.
  - The base and side colour, and the 3D background, are set under **Edit…**.
- **Spectra.** Click a point to see that scan's spectrum, or drag a box to average the spectra inside it. Background mode works the same way, with a box. EICs become 2D maps: type an m/z value, or double-click a peak in the spectrum.
- **Navigation.**
  - The mouse wheel zooms both axes. Hold Shift to zoom ¹tR only, or Ctrl to zoom ²tR only.
  - Right-drag draws a zoom box, and right-click zooms out.
  - ← / → step one modulation; ↑ / ↓ step one scan.
- **Integration (blobs).** In Integrate mode, drag a box around a peak.
  - Within each modulation inside the box, a baseline is drawn across the ²tR slice (a straight line between the box edges by default). The signal above it is summed and multiplied by the scan interval.
  - So **Volume** is in counts·s, the same units as 1D areas.
  - The table also shows the apex ¹tR / ²tR, height, volume %, and the number of modulations the peak spans.
- **Identify.** Library-searches the selected peaks (or all of them) using each peak's spectrum, averaged over the scans at ≥10% of its apex, and fills in Name and Match. Names you have typed yourself are kept.
- **Exports.**
  - **CSV…** writes the 2D matrix: one row per modulation, one column per ²tR point.
  - **Image…** writes a publication figure (PNG/TIFF/SVG/PDF) using the palette, scale and colour range set on screen. Bicubic smoothing, a colour bar and the integrated peak boxes are optional.
  - The peak table exports with ¹tR / ²tR bounds.

### Integration options (Peaks panel)

- **Baseline:**
  - a straight line between the two ends (default)
  - horizontal at the lower end
  - zero
- **Snap ends to valley:** moves each end of a new integration to the lowest point within *n* scans, so you don't have to click exactly on the valley. Set it to 0 to turn snapping off.
- **Peaks are kept per trace.** You can integrate the same peak on the TIC and on several EICs. *Area %* is calculated within each trace. Selecting a row switches the chromatogram back to the trace that peak was integrated on.
- **Area units are counts × seconds** (trapezoidal integration above the baseline). Absolute numbers won't match the values ChemStation or MassHunter report, because they scale areas differently. Ratios between peaks, and calibration curves built from these areas, are unaffected.

### Files with both formats

MassHunter GC/MS Acquisition writes both an `AcqData` folder and a `data.ms` file. The **Data** box in the toolbar switches between them. They contain the same scans, and the app reads both identically. The TIC is the instrument's own recorded value, so CSV exports match Agilent's `tic_front.csv` exactly.


## NMR

Open a Bruker experiment as a **.zip** or as its numbered folder (the one containing `fid` and `acqus`). TopSpin's processed spectrum is used if it's there; otherwise the FID is processed in the app (Fourier transform, automatic phasing, baseline). TopSpin ASCII exports and JCAMP-DX also open.

**¹H:** *Auto analyse* finds each multiplet, integrates it and measures the multiplicity and coupling constants (s, d, t, q, dd, dt, td, ddd …; roofed AB pairs are recognised). Then:

- drag the green edges of a region to adjust it, or drag across the spectrum (mode **2 Integrate**) to add one
- type the number of protons for one signal in the **nH** column; every integral rescales
- edit **Mult.** and **J** where multiplets overlap or are second order
- untick **Report** for solvent, water and impurities (residual CDCl₃ and water are flagged automatically)

**¹³C and DEPT:** peaks are picked automatically. *File ▸ Add DEPT* puts the DEPT-135 underneath and labels every carbon: positive = CH or CH₃, negative = CH₂, absent = quaternary. A DEPT-90 as well separates CH from CH₃.

**Report tab:** the journal-style line, e.g. *¹H NMR (500 MHz, CDCl₃) δ 7.54 (d, J = 7.5 Hz, 2H) …*. *Copy (formatted)* keeps superscripts, subscripts and italic *J* when pasted into Word.

**Processing tab:** phase sliders and auto phase, line broadening and zero filling (raw FIDs), baseline correction, solvent referencing. Mode **4 Reference** sets any clicked peak to a chosen shift.

Always check the automatic multiplicities against the spectrum: they are a starting point, not a replacement for interpretation.

## FT-IR

Opens Bruker OPUS files directly (`.0`, `.1` …), JCAMP-DX and CSV/text.

- **Show** switches between %Transmittance and Absorbance.
- **Find peaks** labels the bands; raise *Sensitivity* for weaker ones. In mode **2 Peaks**, click a band to add or remove it, or drag across a region to label everything in it.
- Each band gets an intensity (s, m, w, relative to the strongest band; br when broad). You can type over it, and type the assignment (e.g. *O–H stretch*) in the last column.
- The **Report** tab gives the experimental-section line, e.g. *IR (ATR) ν<sub>max</sub>/cm⁻¹ 3344 (br), 2929 (s), 2866 (s) …*
- **Overlay…** adds up to four more spectra; *Overlay offset* separates them.
- *Peaks ▸ Baseline correction* removes a sloping baseline (rubber band).

## LC-MS

Opens Thermo `.raw` files directly (no Thermo software needed), and mzML / mzXML from any vendor (convert with ProteoWizard msConvert if needed).

- The **Data** box lists the polarities and MS levels in the file, e.g. *Positive mode · MS*, *Negative mode · MS*, *Positive mode · MS2*. Runs that switch polarity are split so each can be viewed on its own.
- Extracted-ion chromatograms use a **ppm** window (default 5 ppm). Type an m/z, or double-click a peak in the spectrum.
- **m/z from formula…** (Ctrl+F) calculates the exact m/z of [M+H]⁺, [M+Na]⁺, [M−H]⁻, [M+HCOO]⁻ … from a molecular formula, plots its XIC, and reports the ppm error of the matching peak in the spectrum shown.
- Integration, background subtraction, exports and tables work as for GC-MS. (The EI library search is switched off: it doesn't apply to ESI spectra.)

Thermo files are read with the open-source [opentfraw](https://pypi.org/project/opentfraw/) library, which reads the file format independently of Thermo's own (non-redistributable) reader. Centroid m/z values and intensities have matched Thermo's own reader in our checks.

## CSV / text data

Opens comma-, semicolon-, tab- or space-separated files, with or without header lines, including decimal commas. A dialog shows a preview and asks:

- **What is this data?** chromatogram, GCxGC (a single raw trace, or x/y/z = ¹tR, ²tR, intensity), FT-IR, Raman, UV-Vis, fluorescence, mass spectrum, NMR, XRD, or anything else. It makes a guess from the column names and values.
- **Which columns** are x and y (and z). For spectra you can pick several y columns to overlay them.
- Extra details where needed: time in minutes or seconds, GCxGC modulation period, %T or absorbance, NMR nucleus and spectrometer frequency (needed for J in Hz).

The data then open in the matching window: chromatograms and GCxGC in the GC window, NMR in the NMR window, IR in the FT-IR window, everything else in the general x,y window (peak labels, integrals, overlays, exports). Ticking *Remember these choices* skips the questions next time for files with the same columns.

## Comparing samples (up to 5)

*Compare samples* on the start screen, or *File ▸ Compare samples* in the GC window.

- Add up to five runs (.D folders, netCDF, FID `.ch`, Thermo `.raw`, mzML). Choosing a folder that contains several .D folders adds them all.
- **Stacked** or **Overlaid** view with linked zoom; *Normalise heights* scales each to its largest peak.
- **2 Integrate:** drag across a peak in any sample. It is integrated in every sample at once, and marked **n.d.** (not detected) where nothing stands above the noise.
- **Find peaks** does this for every peak above the thresholds in the second toolbar.
- **3 Select:** click a peak (Ctrl-click for more) to highlight it in all samples. Samples without it show a red dashed line and "not detected".
- **Align on selected** lines up retention times on a peak that is in every sample (or type a shift per sample).
- The table shows area, area %, height, or the ratio to a reference peak (right-click a row ▸ *Use as reference*, e.g. an internal standard). For GC-MS, *MS match* compares the apex spectra; low values are shaded because the peaks may be different compounds.
- **2D (GCxGC)** view: side-by-side 2D plots with linked zoom; drag a box around a blob to integrate it in every sample.
- **Mass spectra beside each sample** (GC-MS, LC-MS, GCxGC-MS). *Spectra at* sets what a click does:
  - **Same time / scan** (default): every sample shows its spectrum at that retention time (after any alignment).
  - **Same peak**: every sample shows the top of the same peak, even if it elutes slightly earlier or later; samples without it say so.
  - **Each sample separately**: only the sample you click changes, for comparing different peaks or scans.
- **Background** subtraction for every sample at once: automatic (just before each peak) or at a time you Shift+click.
- **Compare with** picks the reference sample: each spectrum shows its match score (0–999) against it, and *Mirror* draws the reference upside-down underneath.
- **Library search:** *Library hit* shows the best match on each spectrum; *Search all* lists the top five hits for every sample side by side; *Identify peaks* fills a *Library ID* column in the table (and says when samples disagree). It uses the same libraries as the GC-MS window.
- **LC-MS samples** (.raw, mzML, mzXML) can be compared too, with a positive / negative mode switch and a ppm window for extracted-ion traces.
- **Export spectra** saves the spectra stacked as one figure (optionally mirrored) or as a table of m/z against intensity for each sample.

## Figures and reports

Every data window has a **Report** menu.

- **Add … to report** sends the chromatogram, spectrum, peak table, NMR multiplet table or NMR / IR report text to the report builder. Images use the normal image dialog (zoom, labels, size, 600 dpi by default). The title you type there becomes the figure caption.
- **Send … to the figure maker** places the plot on the figure maker's page as a vector graphic.

### Figure maker

- **Pictures:** *Picture…* inserts structures or pictures (PNG, JPEG, SVG). *Paste* (Ctrl+V) inserts a structure copied from a drawing program. You can also drag a file onto the page.
- **Labels and captions:** double-click text to edit it. x² / x₂ (or Ctrl+Shift+= / Ctrl+=) make superscripts and subscripts.
- **Arrows:** → reaction, ↔ resonance, ⇌ equilibrium (e.g. keto–enol tautomerism), ⇒ retrosynthesis, and plain lines. Drag on the page to draw one; drag its ends to point it. Angles snap to 15° unless you hold Shift.
- **Layout:** drag items to move them; drag a picture's corner square to resize it. Arrow keys nudge the selection. Select several items to align them. The page size has presets for journal columns, A4 and slides, or use *Fit page to contents*.
- **Output:** export as PNG / TIFF (600 dpi default), SVG or PDF, copy to the clipboard, or *Add to report*. *Save figure* (.wgfig) keeps it editable. Undo and redo with Ctrl+Z / Ctrl+Y.

### Report builder

- **Templates** (*New*):
  - student laboratory report
  - compound characterisation
  - client analysis report, with fields for report number, client, client point of contact, sample ID(s), dates received and reported, and analyst
  - blank versions of the student and client reports
- **Template prompts:** grey items are prompts and are left out of the export until you edit them.
- **Adding content:** new content goes after the selected item. If nothing is selected, it goes at the end of the *Results* section. *Insert* adds headings, text (bold, italic, superscript, subscript and common symbols), editable tables, images and page breaks. Drag items in *Contents* to reorder them.
- **Export:** PDF or Word (.docx). Figures and tables are numbered automatically. Images keep their full resolution: they are stored losslessly, not as JPEG. The header and footer carry the letterhead (client reports), report number or student name, and page numbers.
- **Letterhead ▸ Logo… / Organisation name and address…** set the logo and the address block for client reports. These are saved on each computer, not in the report file, so the logo file isn't distributed with the app.
- **Save** (.wgreport) keeps the whole report, images included, so you can come back to it.

## Exporting tabulated data

Every table (integrated peaks, GCxGC blobs, NMR multiplets and peaks, IR bands, sample comparisons) has **Export table…**, which saves an Excel workbook (.xlsx), CSV or tab-separated text, and **Copy for report**, which copies a short table. Paste it into Word or PowerPoint and it arrives as a formatted table; paste it into Excel and it fills the cells.

## Files

| File | Contents |
|---|---|
| `wombat.py` | start screen; run this |
| `gcms_viewer.py` | the GC-MS / GC-FID / GCxGC window |
| `nmr.py`, `nmr_view.py` | NMR reading/analysis and the NMR window |
| `tables.py` | Excel/CSV table export and copy-as-table |
| `common.py` | app name, version, shared window code |
| `spectra.py`, `spectrum_view.py` | FT-IR / general spectra: readers (OPUS, JCAMP-DX), band picking, the FT-IR and x,y windows |
| `csv_import.py` | the CSV / text import dialog |
| `lcms.py`, `lcms_view.py` | LC-MS readers (Thermo .raw, mzML, mzXML), exact-mass tools, the LC-MS window |
| `compare_view.py` | the comparison window |
| `export_dialogs.py` | the image-export dialog |
| `drag_labels.py` | draggable plot labels and label auto-arrangement |
| `figure_maker.py` | the figure maker |
| `report_builder.py` | the report builder (PDF and Word export) |
| `gcms_reader.py` | file readers. These can be used on their own in scripts, e.g. `run = read_d("x.D"); run.tic; run.spectrum(i); run.eic(176)` |
| `integration.py` | integration maths |
| `plots_export.py` | publication-figure export (matplotlib) |
| `ms_library.py` | MSP library reader, index and search (usable from scripts too) |
| `nist_search.py` | NIST MS Search hand-off |
| `library_panel.py` | the Library search tab |
| `gcxgc.py` | GCxGC folding and blob integration maths |
| `gcxgc_view.py` | the 2D plot panel |
| `view_widgets.py` | shared zoom/pan plot behaviour |
| `view3d.py` | the synced 3D view |
| `mesh_export.py` | 3D model building and GLB / FBX / STL writers |
| `colormaps.py` | colour schemes |

MassHunter files are read with the open-source [rainbow-api](https://pypi.org/project/rainbow-api/) library. High-resolution MassHunter *profile* data (Q-TOF `MSProfile.bin`) also needs `pip install python-lzf`; GC single-quad data doesn't.

### Licences of the libraries used

numpy, scipy, nmrglue, PyOpenGL (BSD); matplotlib (PSF-style); pyqtgraph, openpyxl, python-docx (MIT); PySide6, rainbow-api (LGPL-3.0); opentfraw (Apache-2.0); brukeropusreader (GPL-3.0).
