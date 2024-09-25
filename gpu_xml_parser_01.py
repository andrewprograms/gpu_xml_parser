import cupy as cp
import cudf
from cudf.core.column import StringColumn
import dask_cudf
from dask.distributed import Client, wait
import dask
import numpy as np
import re

# Initialize Dask client for multi-GPU support
client = Client()  # Adjust scheduler settings as needed

# Define hierarchical classes representing DOCX elements
class DocxElement:
    def __init__(self, attributes):
        self.attributes = attributes

class TextRun(DocxElement):
    def __init__(self, attributes, text):
        super().__init__(attributes)
        self.text = text

class Paragraph(DocxElement):
    def __init__(self, attributes, runs):
        super().__init__(attributes)
        self.runs = runs  # List of TextRun

class TableCell(DocxElement):
    def __init__(self, attributes, paragraphs):
        super().__init__(attributes)
        self.paragraphs = paragraphs  # List of Paragraph

class TableRow(DocxElement):
    def __init__(self, attributes, cells):
        super().__init__(attributes)
        self.cells = cells  # List of TableCell

class Table(DocxElement):
    def __init__(self, attributes, rows):
        super().__init__(attributes)
        self.rows = rows  # List of TableRow

class GpuDocxParser:
    def __init__(self, xml_content):
        # Load XML content directly into GPU memory using cuDF
        self.gpu_data = cudf.Series([xml_content])

    def parse(self):
        # Call the main parsing function
        return self._parse_docx_on_gpu()

    def _parse_docx_on_gpu(self):
        # Split XML content into lines for parallel processing
        lines = self.gpu_data.str.split('\n', expand=True).transpose()

        # Remove empty lines and whitespace
        lines = lines.str.strip()
        lines = lines.dropna()
        lines = lines[lines.str.len() > 0]

        # Use FSM to parse XML tags
        elements = self._parse_elements(lines)

        return elements

    def _parse_elements(self, lines):
        # Initialize state machine variables
        stack = []
        root_elements = []

        # Convert lines to Dask-cuDF for multi-GPU processing
        dask_lines = dask_cudf.from_cudf(lines, npartitions=4)

        # Process lines in parallel
        futures = dask_lines.map_partitions(self._process_lines_partition).persist()
        wait(futures)
        parsed_elements = futures.compute()

        # Flatten list of lists
        parsed_elements = [item for sublist in parsed_elements for item in sublist]

        return parsed_elements

    def _process_lines_partition(self, partition):
        # This function processes a partition of lines
        elements = []
        stack = []

        for line in partition.to_pandas():
            line = line.strip()
            if line.startswith('<') and line.endswith('>'):
                if line.startswith('</'):
                    # Closing tag
                    tag = re.match(r'</([a-zA-Z0-9:]+)>', line).group(1)
                    content = []
                    while stack and stack[-1]['tag'] != tag:
                        content.append(stack.pop())
                    if stack and stack[-1]['tag'] == tag:
                        start_element = stack.pop()
                        attributes = start_element['attributes']
                        # Create appropriate element based on tag
                        element = self._create_element(tag, attributes, content[::-1])
                        if stack:
                            stack.append(element)
                        else:
                            elements.append(element)
                else:
                    # Opening tag with possible attributes
                    tag_match = re.match(r'<([a-zA-Z0-9:]+)([^>]*)>', line)
                    if tag_match:
                        tag = tag_match.group(1)
                        attr_string = tag_match.group(2)
                        attributes = self._extract_attributes(attr_string)
                        stack.append({'tag': tag, 'attributes': attributes})
            else:
                # Text content
                if stack:
                    # Assume it's text within the current element
                    text = line
                    stack.append(text)
        return elements

    def _extract_attributes(self, attr_string):
        # Extract attributes using regex
        attributes = {}
        matches = re.findall(r'([a-zA-Z0-9:]+)="([^"]+)"', attr_string)
        for key, value in matches:
            attributes[key] = value
        return attributes

    def _create_element(self, tag, attributes, content):
        # Create hierarchical element based on tag
        if tag == 'w:t':
            # Text Run
            text_content = ''.join(content) if content else ''
            return TextRun(attributes, text_content)
        elif tag == 'w:r':
            # Run
            runs = [item for item in content if isinstance(item, TextRun)]
            return runs  # Return list of TextRuns
        elif tag == 'w:p':
            # Paragraph
            runs = []
            for item in content:
                if isinstance(item, list):
                    runs.extend(item)
            return Paragraph(attributes, runs)
        elif tag == 'w:tc':
            # Table Cell
            paragraphs = [item for item in content if isinstance(item, Paragraph)]
            return TableCell(attributes, paragraphs)
        elif tag == 'w:tr':
            # Table Row
            cells = [item for item in content if isinstance(item, TableCell)]
            return TableRow(attributes, cells)
        elif tag == 'w:tbl':
            # Table
            rows = [item for item in content if isinstance(item, TableRow)]
            return Table(attributes, rows)
        else:
            # Other elements can be added as needed
            return DocxElement(attributes)

def main():
    # Example XML content from a DOCX file
    xml_content = """
    <w:document>
        <w:body>
            <w:tbl>
                <w:tr w:trHeight="300">
                    <w:tc w:tcW="5000"><w:p><w:r><w:t>Cell 1</w:t></w:r></w:p></w:tc>
                    <w:tc w:tcW="5000"><w:p><w:r><w:t>Cell 2</w:t></w:r></w:p></w:tc>
                </w:tr>
                <w:tr w:trHeight="400">
                    <w:tc w:tcW="5000"><w:p><w:r><w:t>Cell 3</w:t></w:r></w:p></w:tc>
                    <w:tc w:tcW="5000"><w:p><w:r><w:t>Cell 4</w:t></w:r></w:p></w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>
    """

    # Create an instance of the parser
    parser = GpuDocxParser(xml_content)

    # Parse the XML content on the GPU
    extracted_elements = parser.parse()

    # Print the extracted elements
    for element in extracted_elements:
        print_element(element)

def print_element(element, indent=0):
    space = ' ' * indent
    if isinstance(element, Table):
        print(f"{space}Table: {element.attributes}")
        for row in element.rows:
            print_element(row, indent + 2)
    elif isinstance(element, TableRow):
        print(f"{space}Row: {element.attributes}")
        for cell in element.cells:
            print_element(cell, indent + 4)
    elif isinstance(element, TableCell):
        print(f"{space}Cell: {element.attributes}")
        for paragraph in element.paragraphs:
            print_element(paragraph, indent + 6)
    elif isinstance(element, Paragraph):
        print(f"{space}Paragraph: {element.attributes}")
        for run in element.runs:
            print_element(run, indent + 8)
    elif isinstance(element, TextRun):
        print(f"{space}TextRun: {element.attributes}, Text: {element.text}")
    else:
        print(f"{space}Element: {element.attributes}")

if __name__ == "__main__":
    main()
