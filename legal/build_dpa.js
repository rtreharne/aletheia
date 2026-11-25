// Builds tool/static/tool/legal/aletheia-dpa-template.docx. Run: npm install docx && node legal/build_dpa.js tool/static/tool/legal/aletheia-dpa-template.docx
const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType, ShadingType,
        AlignmentType, LevelFormat, BorderStyle, PageBreak, Footer, PageNumber, HeadingLevel } = require("docx");

const FONT = "Georgia";
// Text with [placeholders] highlighted yellow.
function runs(text, opts = {}) {
  return text.split(/(\[[^\]]+\])/).filter(Boolean).map(part =>
    part.startsWith("[") ? new TextRun({ text: part, highlight: "yellow", font: FONT, ...opts })
                         : new TextRun({ text: part, font: FONT, ...opts }));
}
const p = (text, o = {}) => new Paragraph({ children: runs(text, o.run), spacing: { after: 120 }, ...o.para });
const h1 = t => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun({ text: t, font: FONT })] });
const h2 = t => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun({ text: t, font: FONT })] });
let listId = 0;
const lists = [];
function clauses(items) {   // numbered sub-clauses, restarting for each clause
  const ref = "c" + (listId++);
  lists.push({ reference: ref, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
    style: { paragraph: { indent: { left: 567, hanging: 567 } } } }] });
  return items.map(t => new Paragraph({ numbering: { reference: ref, level: 0 }, children: runs(t), spacing: { after: 100 } }));
}
function bullets(items) {
  return items.map(t => new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: runs(t), spacing: { after: 80 } }));
}
const border = { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF" };
const borders = { top: border, bottom: border, left: border, right: border };
function table(widths, rows, header = true, keep = false) {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: total, type: WidthType.DXA }, columnWidths: widths,
    rows: rows.map((r, i) => new TableRow({ cantSplit: true, tableHeader: header && i === 0, children: r.map((c, j) => new TableCell({
      width: { size: widths[j], type: WidthType.DXA }, borders,
      shading: header && i === 0 ? { type: ShadingType.CLEAR, fill: "F2F2F2", color: "auto" } : undefined,
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: String(c).split("\n").map(line => new Paragraph({ keepNext: keep && i < rows.length - 1, children: runs(line, header && i === 0 ? { bold: true } : {}) })),
    })) })),
  });
}
const W = 9026; // A4 width minus 1" margins

const body = [
  new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun({ text: "Data processing agreement", font: FONT })] }),
  p("Aletheia · Template, version [version] · [date]", { run: { color: "666666" } }),
  p("This template is offered for convenience. Fields in [square brackets] are to be completed. It is not legal advice; please have it reviewed before signing.", { run: { italics: true, color: "666666" } }),

  h1("Parties"),
  p("(1) [Institution name] of [Institution address] (the Controller); and"),
  p("(2) [Company name], a company registered in England and Wales under number [company number], whose registered office is at [registered address] (the Processor)."),
  p("This agreement forms part of the Processor's terms of service (the Terms) for each order and pilot of Aletheia placed by the Controller (each an Order). Where this agreement and the Terms conflict on the protection of personal data, this agreement prevails."),

  h1("1. Definitions"),
  ...clauses([
    "Data Protection Law means the UK GDPR, the Data Protection Act 2018, and any other law on the processing of personal data that applies to either party.",
    "Controller, Processor, Data Subject, Personal Data, Personal Data Breach and Processing have the meanings given in the UK GDPR.",
    "Controller Data means Personal Data processed by the Processor on behalf of the Controller in providing Aletheia, as described in Annex 1.",
    "Sub-processor means a third party engaged by the Processor to process Controller Data.",
    "Working day means Monday to Friday, excluding public holidays in England.",
  ]),

  h1("2. Roles and instructions"),
  ...clauses([
    "The Controller is the controller and the Processor is the processor of Controller Data. Annex 1 sets out the subject matter, duration, nature and purpose of the processing, and the types of Personal Data and categories of Data Subjects.",
    "The Processor will process Controller Data only on the Controller's documented instructions, including with regard to transfers outside the United Kingdom, unless required to do otherwise by law, in which case it will tell the Controller first unless the law forbids it. This agreement, the Terms, each Order and the Controller's use of Aletheia's settings are the Controller's instructions.",
    "The Processor will tell the Controller immediately if, in its opinion, an instruction infringes Data Protection Law.",
    "The Controller is responsible for having a lawful basis for the processing and for giving Data Subjects the information required by Data Protection Law, including that their work will be checked using Aletheia and an AI model.",
    "The Processor will not use Controller Data for any other purpose, including training or improving AI models, and will not sell it. The Processor's contract with its AI Sub-processor prohibits that Sub-processor from using Controller Data to train its models.",
  ]),

  h1("3. Confidentiality and security"),
  ...clauses([
    "The Processor will ensure that everyone it authorises to process Controller Data is bound by a duty of confidentiality.",
    "The Processor will implement and maintain the technical and organisational measures in Annex 2, and any others appropriate to the risk, as required by Article 32 of the UK GDPR. The Processor may update these measures provided the overall level of protection is not reduced.",
  ]),

  h1("4. Sub-processors"),
  ...clauses([
    "The Controller gives general authorisation for the Processor to engage the Sub-processors listed in Annex 3.",
    "The Processor will give the Controller at least 30 days' written notice before adding or replacing a Sub-processor. The Controller may object on reasonable data protection grounds within that period. If the parties cannot resolve the objection, the Controller may end the affected Order by written notice and receive a refund of fees paid for the rest of the academic year, in proportion to the time left.",
    "The Processor will impose on each Sub-processor, by written contract, data protection obligations that give at least the protection required by this agreement, and remains liable to the Controller for each Sub-processor's performance of those obligations.",
  ]),

  h1("5. International transfers"),
  ...clauses([
    "The Processor will transfer Controller Data outside the United Kingdom only to the Sub-processors and locations listed in Annex 3, or as otherwise instructed by the Controller, and only where the transfer complies with Chapter V of the UK GDPR.",
    "For the transfer to Anthropic, PBC in the United States, the safeguard is [transfer mechanism, e.g. the UK International Data Transfer Addendum to the EU Standard Contractual Clauses incorporated in Anthropic's data processing addendum].",
  ]),

  h1("6. Assistance"),
  ...clauses([
    "Taking into account the nature of the processing, the Processor will assist the Controller by appropriate technical and organisational measures, insofar as possible, to respond to requests from Data Subjects to exercise their rights. The Processor will pass any such request it receives directly to the Controller within 5 working days and will not respond itself unless the Controller instructs it to.",
    "The Processor will provide reasonable assistance with the Controller's obligations on security, Personal Data Breach notification, data protection impact assessments and prior consultation with the Information Commissioner, taking into account the nature of the processing and the information available to the Processor.",
  ]),

  h1("7. Personal Data Breaches"),
  ...clauses([
    "The Processor will notify the Controller without undue delay, and in any event within 48 hours, after becoming aware of a Personal Data Breach affecting Controller Data, by email to the Controller's contact in Annex 1.",
    "The notification will describe, as far as then known, the nature of the breach, the categories and approximate number of Data Subjects and records concerned, the likely consequences, and the measures taken or proposed. The Processor will provide further information as it becomes available and will take reasonable steps to contain and remedy the breach.",
  ]),

  h1("8. Retention, deletion and return"),
  ...clauses([
    "During each Order, the Processor will delete Controller Data on 31 August a year after the end of the academic year in which it was created (for example, data from the 2026–27 academic year is deleted on 31 August 2028), or earlier on the Controller's written instruction.",
    "Within 30 days after the last Order ends, the Processor will delete all remaining Controller Data, or, if the Controller asks in writing before then, return it in a commonly used format and then delete it, unless the law requires the Processor to keep it. Copies in backups are deleted as the backups expire, within [backup retention period].",
    "On request, the Processor will confirm deletion in writing.",
  ]),

  h1("9. Information and audit"),
  ...clauses([
    "The Processor will make available to the Controller all information reasonably necessary to demonstrate compliance with Article 28 of the UK GDPR, including completed security questionnaires.",
    "The Processor will allow for and contribute to audits, including inspections, by the Controller or an auditor it appoints who is bound by confidentiality, on at least 30 days' notice, during working hours, and not more than once in any 12 months unless required by a regulator or following a Personal Data Breach. Each party bears its own costs.",
  ]),

  h1("10. Liability and term"),
  ...clauses([
    "Each party's liability under this agreement is subject to the limitations in the Terms, except where Data Protection Law does not allow liability to be limited.",
    "This agreement continues for as long as the Processor processes Controller Data.",
    "This agreement is governed by the law of England and Wales, and the courts of England and Wales have exclusive jurisdiction.",
  ]),

  h1("Signed"),
  table([W / 2, W / 2], [
    ["For the Controller", "For the Processor"],
    ["Name: [name]", "Name: [name]"],
    ["Position: [position]", "Position: [position]"],
    ["Signature:", "Signature:"],
    ["Date: [date]", "Date: [date]"],
  ], true, true),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Annex 1. Details of the processing"),
  table([2600, W - 2600], [
    ["Item", "Details"],
    ["Subject matter", "Providing Aletheia, a tool used through the Controller's learning management system (LMS) by LTI 1.3, which asks students timed questions about their own submitted work."],
    ["Duration", "For each Order, the academic year (1 September to 31 August), followed by the retention period in clause 8."],
    ["Nature of the processing", "Collection from the LMS, storage, text extraction, sending submission text to an AI model to write learning objectives, questions and explanations, recording answers and timings, display to students and staff, return of the uploaded file to the LMS gradebook where the LMS supports it, and deletion."],
    ["Purpose", "To let the Controller's staff check that students understand the work they hand in, as evidence to weigh alongside their own judgement. Aletheia does not make decisions about students and never sets or changes grades."],
    ["Categories of Data Subjects", "Students and staff (instructors and teaching assistants) in the Controller's courses that use Aletheia."],
    ["Types of Personal Data", "Students: name (or email address if no name is provided), LMS user ID, course role, course and assignment names and IDs, uploaded files and their extracted text, confirmation and AI-policy agreement with times, learning objectives, questions, answers, answer times, alignment scores, and an integrity log (leaving the page, switching windows, copy, cut, paste, right-click, room-word check results), with times.\nStaff: name, LMS user ID, course role, assignment settings, and invitations to students to try again."],
    ["Special category data", "None is required. Submissions may incidentally contain special category data, depending on their subject. The Controller decides what students are asked to submit."],
    ["Frequency", "Continuous during each Order."],
    ["Controller's contact for notices", "[name, role, email]"],
    ["Processor's contact", "[name], [email]"],
  ]),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Annex 2. Technical and organisational measures"),
  h2("Access and authentication"),
  ...bullets([
    "Students and staff reach Aletheia only through the Controller's LMS, using LTI 1.3: each launch is a signed message, verified against the LMS's published keys, with checks on issuer, audience, nonce, expiry and deployment. There are no separate Aletheia passwords.",
    "Students can see only their own attempts. Staff status comes only from roles in the course itself, and staff can see only courses they have launched from. Observer roles (for example, parents) are refused.",
    "Files are returned to the LMS only through signed links that expire.",
    "The Processor's administrative access is limited to [named personnel], protected by [multi-factor authentication], and logged.",
  ]),
  h2("Encryption"),
  ...bullets([
    "All traffic is encrypted in transit with TLS (HTTPS only). Session and security cookies are marked Secure.",
    "Data at rest is encrypted by [hosting provider] using [encryption at rest, e.g. AES-256].",
  ]),
  h2("Hosting and resilience"),
  ...bullets([
    "Aletheia and its database and files are hosted by [hosting provider] in [hosting location], which holds [certifications, e.g. ISO 27001].",
    "Backups are taken [backup frequency], encrypted, kept in [backup location] and retained for [backup retention period].",
  ]),
  h2("Data minimisation"),
  ...bullets([
    "Only the extracted text of a submission, the assignment title and the instructor's brief are sent to the AI Sub-processor. Students' names and LMS IDs are not sent.",
    "No analytics, advertising or third-party tracking is used. Fonts and other resources are served by Aletheia itself.",
    "Controller Data is deleted on the schedule in clause 8.",
  ]),
  h2("Organisation"),
  ...bullets([
    "Personnel with access to Controller Data are bound by confidentiality and trained in data protection.",
    "Software dependencies are kept up to date, and security fixes are applied [within a stated period].",
    "Personal Data Breaches are handled under a documented incident response procedure, as in clause 7.",
  ]),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Annex 3. Authorised Sub-processors"),
  table([2300, 3026, 1700, 2000], [
    ["Sub-processor", "Processing", "Location", "Transfer safeguard"],
    ["Anthropic, PBC", "Writes learning objectives, questions and explanations from submission text (Claude API).", "United States", "[transfer mechanism]"],
    ["[Hosting provider]", "Hosts the application, database and files.", "[hosting location]", "[None needed if UK, or UK adequacy regulations if EU]"],
  ]),
  p(""),
  p("The Processor's email and video-call providers handle only enquiries from prospective customers and are not Sub-processors of Controller Data."),
];

const doc = new Document({
  creator: "Aletheia", title: "Aletheia data processing agreement (template)",
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", run: { font: FONT, size: 40 }, paragraph: { spacing: { before: 0, after: 120 }, keepNext: true } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 26, bold: true }, paragraph: { spacing: { before: 300, after: 120 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 22, bold: true }, paragraph: { spacing: { before: 200, after: 80 }, keepNext: true, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 567, hanging: 283 } } } }] },
    ...lists,
  ] },
  sections: [{
    properties: { page: { margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "Aletheia data processing agreement · page ", font: FONT, size: 16, color: "808080" }),
      new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16, color: "808080" }),
    ] })] }) },
    children: body,
  }],
});
Packer.toBuffer(doc).then(buf => fs.writeFileSync(process.argv[2], buf));
