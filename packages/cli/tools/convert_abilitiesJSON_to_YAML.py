import json
import yaml
import os
import re

def sanitize_filename(name):
    """Sanitize the ability name to create a valid filename"""
    # Remove special characters and replace spaces with hyphens
    sanitized = re.sub(r'[^\w\s-]', '', name.lower())
    sanitized = re.sub(r'[-\s]+', '-', sanitized)
    return sanitized.strip('-')

def convert_json_to_yaml_files(json_file_path, output_directory):
    """
    Convert a JSON file with multiple abilities to individual YAML files
    in the target format
    """
    
    # Create output directory if it doesn't exist
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)
    
    # Load the JSON data
    with open(json_file_path, 'r') as f:
        abilities = json.load(f)
    
    # Convert each ability to the target YAML format
    for ability in abilities:
        # Create the target format structure
        yaml_ability = {
            'id': ability['ability_id'],
            'name': ability['name'],
            'description': ability['description'],
            'tactic': ability['tactic'],
            'technique': {
                'attack_id': ability['technique_id'],
                'name': ability['technique_name']
            },
            'repeatable': True,
            'platforms': {
                'linux': {
                    'sh': {
                        'command': ability['executors'][0]['command'],
                        'timeout': 30  # Default timeout
                    }
                }
            }
        }
        
        # Add parsers if they exist in the original
        if 'parsers' in ability['executors'][0]:
            yaml_ability['platforms']['linux']['sh']['parsers'] = {}
            for parser in ability['executors'][0]['parsers']:
                parser_module = parser['module']
                yaml_ability['platforms']['linux']['sh']['parsers'][parser_module] = []
                
                for parserconfig in parser['parserconfigs']:
                    yaml_ability['platforms']['linux']['sh']['parsers'][parser_module].append({
                        'source': parserconfig['source']
                    })
        
        # Adjust timeout based on command type
        command = ability['executors'][0]['command'].lower()
        if any(keyword in command for keyword in ['nmap', 'scan', 'ssh', 'curl', 'wget']):
            yaml_ability['platforms']['linux']['sh']['timeout'] = 60
        elif 'sleep' in command or 'wait' in command:
            # Extract timeout from sleep commands if possible
            sleep_match = re.search(r'sleep\s+(\d+)', command)
            if sleep_match:
                yaml_ability['platforms']['linux']['sh']['timeout'] = int(sleep_match.group(1)) + 10
        
        # Generate filename
        filename = f"{sanitize_filename(ability['name'])}.yml"
        filepath = os.path.join(output_directory, filename)
        
        # Write to YAML file (wrapped in a list as per your example)
        with open(filepath, 'w') as f:
            yaml.dump([yaml_ability], f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
        
        print(f"✓ Created: {filename}")

def batch_convert_directory(input_directory, output_directory):
    """Convert all JSON files in a directory"""
    for filename in os.listdir(input_directory):
        if filename.endswith('.json'):
            json_file_path = os.path.join(input_directory, filename)
            specific_output_dir = os.path.join(output_directory, os.path.splitext(filename)[0])
            convert_json_to_yaml_files(json_file_path, specific_output_dir)

def main():
    # Configuration
    input_json_file = 'abilities.json'  # Your input JSON file
    output_dir = 'converted_abilities'  # Output directory
    
    print("Starting conversion from JSON to individual YAML files...")
    
    try:
        # Convert the files
        convert_json_to_yaml_files(input_json_file, output_dir)
        print(f"\n✅ Conversion complete! {len(os.listdir(output_dir))} files saved in '{output_dir}' directory.")
        
    except FileNotFoundError:
        print(f"❌ Error: Input file '{input_json_file}' not found.")
    except json.JSONDecodeError:
        print("❌ Error: Invalid JSON format in the input file.")
    except Exception as e:
        print(f"❌ An error occurred: {str(e)}")

if __name__ == "__main__":
    main()